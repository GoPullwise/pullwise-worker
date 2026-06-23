from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

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


def package_script_line(checkout_dir: Path, script: str) -> int:
    package_path = checkout_dir / "package.json"
    package_text = read_repository_text_file(package_path)
    if package_text is None:
        return 1
    return first_matching_line(package_text, rf'"{re.escape(script)}"\s*:')


def install_evidence_file(checkout_dir: Path) -> str:
    for filename in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"):
        if repository_regular_file(checkout_dir / filename):
            return filename
    return "package.json"


def run_deterministic_repository_checks(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    findings.extend(readme_missing_package_script_findings(job, checkout_dir))
    findings.extend(workflow_missing_package_script_findings(job, checkout_dir))
    findings.extend(dockerfile_missing_source_findings(job, checkout_dir))
    findings.extend(committed_secret_findings(job, checkout_dir))
    return findings[:25]


def readme_missing_package_script_findings(job: dict, checkout_dir: Path) -> list[dict]:
    package_path = checkout_dir / "package.json"
    if not repository_regular_file(package_path):
        return []
    readme_path = first_existing_file(checkout_dir, ["README.md", "README.markdown", "README"])
    if readme_path is None:
        return []

    package_text = read_repository_text_file(package_path)
    readme_text = read_repository_text_file(readme_path)
    if package_text is None or readme_text is None:
        return []
    try:
        package_data = json.loads(package_text)
    except json.JSONDecodeError:
        return []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    if not isinstance(scripts, dict):
        return []
    defined_scripts = {str(name) for name in scripts if isinstance(name, str)}
    if not defined_scripts:
        return []

    readme_rel = readme_path.relative_to(checkout_dir).as_posix()
    package_rel = "package.json"
    scripts_line = first_matching_line(package_text, r'"scripts"\s*:')
    seen: set[str] = set()
    findings: list[dict] = []
    for line_number, line in enumerate(readme_text.splitlines(), start=1):
        for match in _README_PACKAGE_SCRIPT_RE.finditer(line):
            manager = match.group(1)
            script = match.group(2)
            if script in defined_scripts or script in seen:
                continue
            seen.add(script)
            replacement_script = package_script_fix_replacement(script, defined_scripts)
            replacement_line = package_script_replacement_line(line, match, replacement_script)
            findings.append(
                missing_package_script_finding(
                    job=job,
                    manager=manager,
                    script=script,
                    readme_file=readme_rel,
                    readme_line=line_number,
                    package_file=package_rel,
                    package_line=scripts_line,
                    replacement_script=replacement_script,
                    source_line=line,
                    replacement_line=replacement_line,
                )
            )
    return findings


def workflow_missing_package_script_findings(job: dict, checkout_dir: Path) -> list[dict]:
    package_path = checkout_dir / "package.json"
    workflows_dir = checkout_dir / ".github" / "workflows"
    if not repository_regular_file(package_path) or not workflows_dir.is_dir():
        return []

    package_text = read_repository_text_file(package_path)
    if package_text is None:
        return []
    try:
        package_data = json.loads(package_text)
    except json.JSONDecodeError:
        return []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    defined_scripts = {str(name) for name in scripts if isinstance(name, str)} if isinstance(scripts, dict) else set()
    package_rel = "package.json"
    scripts_line = first_matching_line(package_text, r'"scripts"\s*:') if isinstance(scripts, dict) else 1
    seen: set[tuple[str, str]] = set()
    findings: list[dict] = []
    for workflow_path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        if not repository_regular_file(workflow_path):
            continue
        workflow_text = read_repository_text_file(workflow_path)
        if workflow_text is None:
            continue
        workflow_rel = workflow_path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(workflow_text.splitlines(), start=1):
            for match in _README_PACKAGE_SCRIPT_RE.finditer(line):
                manager = match.group(1)
                script = match.group(2)
                if script in defined_scripts:
                    continue
                key = (workflow_rel, script)
                if key in seen:
                    continue
                seen.add(key)
                replacement_script = package_script_fix_replacement(script, defined_scripts)
                replacement_line = package_script_replacement_line(line, match, replacement_script)
                findings.append(
                    missing_workflow_package_script_finding(
                        job=job,
                        manager=manager,
                        script=script,
                        workflow_file=workflow_rel,
                        workflow_line=line_number,
                        package_file=package_rel,
                        package_line=scripts_line,
                        replacement_script=replacement_script,
                        source_line=line,
                        replacement_line=replacement_line,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def dockerfile_missing_source_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for dockerfile_path in iter_dockerfiles(checkout_dir):
        dockerfile_text = read_repository_text_file(dockerfile_path)
        if dockerfile_text is None:
            continue
        dockerfile_rel = dockerfile_path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(dockerfile_text.splitlines(), start=1):
            parsed = dockerfile_copy_add_sources(line)
            if not parsed:
                continue
            instruction, sources = parsed
            for source in sources:
                if not docker_source_is_static_local(source):
                    continue
                if docker_source_exists(checkout_dir, source):
                    continue
                key = (dockerfile_rel, source)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    missing_dockerfile_source_finding(
                        job=job,
                        dockerfile_file=dockerfile_rel,
                        dockerfile_line=line_number,
                        instruction=instruction,
                        source=source,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


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


def dockerfile_copy_add_sources(line: str) -> tuple[str, list[str]] | None:
    text = line.strip()
    if not text or text.startswith("#") or text.endswith("\\"):
        return None
    match = re.match(r"^(COPY|ADD)\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return None
    instruction = match.group(1).upper()
    body = dockerfile_strip_inline_comment(match.group(2).strip())
    body = dockerfile_strip_instruction_flags(body)
    if not body or "--from=" in match.group(2):
        return None
    if body.startswith("["):
        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(items, list) or len(items) < 2 or not all(isinstance(item, str) for item in items):
            return None
        return instruction, items[:-1]
    try:
        tokens = shlex.split(body, posix=True)
    except ValueError:
        return None
    if len(tokens) < 2:
        return None
    return instruction, tokens[:-1]


def dockerfile_strip_inline_comment(value: str) -> str:
    if " #" not in value:
        return value
    return value.split(" #", 1)[0].strip()


def dockerfile_strip_instruction_flags(value: str) -> str:
    remaining = value.strip()
    while remaining.startswith("--"):
        parts = remaining.split(maxsplit=1)
        if len(parts) < 2:
            return ""
        remaining = parts[1].strip()
    return remaining


def docker_source_is_static_local(source: str) -> bool:
    source = str(source or "").strip()
    if not source or source in {".", "./"}:
        return False
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "git://")):
        return False
    if "$" in source or any(char in source for char in "*?[]"):
        return False
    if source.startswith("/") or _WINDOWS_DRIVE_RE.match(source):
        return False
    parts = [part for part in source.replace("\\", "/").split("/") if part]
    return ".." not in parts


def docker_source_exists(checkout_dir: Path, source: str) -> bool:
    normalized = source.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    try:
        target = (checkout_dir / normalized).resolve(strict=False)
        root = checkout_dir.resolve(strict=False)
    except OSError:
        return False
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return repository_path_exists_without_following_symlink(target)


def first_existing_file(root: Path, names: list[str]) -> Path | None:
    for name in names:
        candidate = root / name
        if repository_regular_file(candidate):
            return candidate
    return None


def first_matching_line(text: str, pattern: str) -> int:
    compiled = re.compile(pattern)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if compiled.search(line):
            return line_number
    return 1


_PACKAGE_SCRIPT_FIX_PREFERENCES = {
    "ci": ["test", "check", "lint", "build"],
    "dev": ["start", "build"],
    "serve": ["start", "dev", "preview", "build"],
    "start": ["dev", "build"],
    "test": ["check", "build"],
    "lint": ["check"],
    "type-check": ["typecheck", "check"],
    "typecheck": ["check"],
    "check": ["test", "lint", "typecheck", "build"],
    "build": ["check"],
}


def package_script_fix_replacement(missing_script: str, defined_scripts: set[str]) -> str:
    available = sorted(
        script
        for script in defined_scripts
        if isinstance(script, str)
        and script != missing_script
        and re.fullmatch(r"[A-Za-z0-9:_-]+", script)
    )
    if not available:
        return ""
    if len(available) == 1:
        return available[0]
    for candidate in _PACKAGE_SCRIPT_FIX_PREFERENCES.get(missing_script, []):
        if candidate in available:
            return candidate
    return ""


def package_script_replacement_line(line: str, match: object, replacement_script: str) -> str:
    if not replacement_script:
        return ""
    try:
        return f"{line[:match.start(2)]}{replacement_script}{line[match.end(2):]}"
    except (AttributeError, IndexError):
        return ""


def committed_secret_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen_locations: set[tuple[str, str]] = set()
    for path in iter_secret_scan_files(checkout_dir):
        text = read_repository_text_file(path)
        if text is None:
            continue
        file_path = path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern in _SECRET_PATTERNS:
                match = pattern["regex"].search(line)
                if not match:
                    continue
                secret_value = match.group(0)
                if secret_match_is_placeholder(secret_value, line):
                    continue
                location_key = (str(pattern["kind"]), file_path)
                if location_key in seen_locations:
                    continue
                seen_locations.add(location_key)
                findings.append(
                    committed_secret_finding(
                        job=job,
                        secret_kind=str(pattern["kind"]),
                        secret_label=str(pattern["label"]),
                        safe_prefix=secret_safe_prefix(secret_value),
                        file_path=file_path,
                        line=line_number,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def iter_secret_scan_files(checkout_dir: Path):
    scanned = 0
    for path in checkout_dir.rglob("*"):
        if scanned >= _SECRET_SCAN_MAX_FILES:
            return
        if not secret_scan_file_allowed(path, checkout_dir):
            continue
        scanned += 1
        yield path


def secret_scan_file_allowed(path: Path, checkout_dir: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        stat_result = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(stat_result.st_mode):
        return False
    try:
        relative = path.relative_to(checkout_dir)
    except ValueError:
        return False
    size = stat_result.st_size
    if size <= 0 or size > _SECRET_SCAN_MAX_BYTES:
        return False
    parts = [part.lower() for part in relative.parts]
    if any(part in _SECRET_SCAN_SKIP_DIRS for part in parts[:-1]):
        return False
    name = relative.name
    lower_name = name.lower()
    if name in _SECRET_SCAN_SKIP_FILES or lower_name in {item.lower() for item in _SECRET_SCAN_SKIP_FILES}:
        return False
    if any(marker in lower_name for marker in ("example", "sample", "fixture", "mock")):
        return False
    if lower_name in {"dockerfile", ".env", ".npmrc", ".pypirc"}:
        return True
    return path.suffix.lower() in _SECRET_SCAN_TEXT_SUFFIXES


def secret_match_is_placeholder(secret_value: str, line: str) -> bool:
    lowered_line = line.lower()
    if any(marker in lowered_line for marker in ("example", "sample", "dummy", "fake", "placeholder", "changeme")):
        return True
    body = re.sub(r"[^A-Za-z0-9]", "", secret_value)
    return len(set(body)) < 8


def secret_safe_prefix(secret_value: str) -> str:
    if secret_value.startswith("sk_live_"):
        return "sk_live_"
    if secret_value.startswith("xox") and "-" in secret_value:
        return secret_value.split("-", 1)[0] + "-"
    if secret_value.startswith("gh") and len(secret_value) >= 4:
        return secret_value[:4]
    return secret_value[:4]


def shell_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "'\"'\"'") + "'"


def committed_secret_finding(
    *,
    job: dict,
    secret_kind: str,
    secret_label: str,
    safe_prefix: str,
    file_path: str,
    line: int,
) -> dict:
    commit = str(job.get("commit") or "the scanned commit")
    digest_input = f"{job.get('repo')}:{commit}:{secret_kind}:{file_path}:{line}".encode("utf-8")
    finding_id = f"static_committed_secret_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    grep_command = f'git grep -n "{safe_prefix}" -- {shell_quote(file_path)}'
    return {
        "id": finding_id,
        "severity": "high",
        "category": "Security",
        "title": f"Committed {secret_label} detected",
        "summary": (
            f"`{file_path}` line {line} contains a value matching a vendor-specific {secret_label} pattern. "
            "The raw value is redacted from this report."
        ),
        "impact": (
            "A committed credential can be copied from repository history and used outside the application until it is revoked or rotated."
        ),
        "detectionReasoning": (
            f"A deterministic secret rule matched the {secret_label} prefix `{safe_prefix}` at commit `{commit}`. "
            "The scanner reports only the location and prefix so the report does not leak the full credential."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{file_path}` line {line} or run `{grep_command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "A deterministic scanner matched a high-confidence credential pattern in a repository file; no provider API validation was attempted."
        ),
        "affectedLocations": [{"file": file_path, "startLine": line, "endLine": line}],
        "evidence": [
            {
                "type": "code",
                "label": "redacted secret location",
                "summary": f"Line {line} contains a {secret_label}-shaped value with prefix `{safe_prefix}`; full value redacted.",
                "file": file_path,
                "startLine": line,
                "endLine": line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            }
        ],
        "reproduction": {
            "commands": [grep_command],
            "input": f"Inspect `{file_path}` line {line}.",
            "expected": "No live credential-like token is committed to the repository.",
            "actual": f"A {secret_label}-shaped token with prefix `{safe_prefix}` is present; full value redacted.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The value matches a vendor-specific {secret_label} token prefix and length.",
            f"The finding points to a concrete repository file and line: `{file_path}:{line}`.",
            "The scanner excludes common docs, examples, fixtures, tests, vendor directories, and lockfiles before reporting.",
        ],
        "limitations": [
            "The scanner does not call the provider API, so it cannot prove whether the credential is still active.",
            "If this was an intentionally revoked test credential, the immediate production impact may be lower.",
        ],
        "file": file_path,
        "line": line,
        "confidence": 0.95,
        "confidenceRationale": (
            "Vendor-specific live-token syntax plus an exact file and line gives high static confidence; active exploitability depends on whether the credential is still valid."
        ),
        "autoFix": False,
        "effort": "review required",
        "fixBenefits": "Removes a committed credential exposure and gives maintainers a precise location to inspect and rotate.",
        "fixRisks": "Removing the line is not enough if the secret was already exposed; rotate or revoke it with the provider.",
        "tags": ["deterministic", "static-proof", "secret", secret_kind],
        "steps": [
            f"Inspect `{file_path}` line {line} at the pinned commit.",
            "Revoke or rotate the credential in the provider console.",
            "Move the value to a secret manager or runtime environment variable.",
            "Remove the committed value and consider history cleanup if the repository was shared.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def missing_package_script_finding(
    *,
    job: dict,
    manager: str,
    script: str,
    readme_file: str,
    readme_line: int,
    package_file: str,
    package_line: int,
    replacement_script: str,
    source_line: str,
    replacement_line: str,
) -> dict:
    command = f"{manager} run {script}"
    replacement_command = f"{manager} run {replacement_script}" if replacement_script else ""
    auto_fix = bool(source_line and replacement_line and source_line != replacement_line)
    bad_code = [{"ln": readme_line, "code": source_line, "t": "del"}] if auto_fix else []
    good_code = [{"ln": readme_line, "code": replacement_line, "t": "add"}] if auto_fix else []
    severity = "medium" if script in _HIGH_SIGNAL_PACKAGE_SCRIPTS else "low"
    digest_input = f"{readme_file}:{readme_line}:{package_file}:{script}".encode("utf-8")
    finding_id = f"static_missing_script_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    repo = str(job.get("repo") or "repository")
    commit = str(job.get("commit") or "the scanned commit")
    return {
        "id": finding_id,
        "severity": severity,
        "category": "Docs",
        "title": f"README references missing package script `{script}`",
        "summary": (
            f"The README tells users to run `{command}`, but the root package.json scripts "
            f"object does not define `{script}`."
        ),
        "impact": (
            "Users following the documented setup or verification path can hit an immediate package-manager "
            "failure before the application starts."
        ),
        "detectionReasoning": (
            f"Static repository check compared `{readme_file}` with the root `{package_file}` scripts object "
            f"at commit `{commit}` and found no `{script}` entry."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{readme_file}` line {readme_line} and `{package_file}` line "
            f"{package_line}; then run `{command}` from the repository root to verify the documented command."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The README command and package.json scripts were compared statically; no project scripts were executed."
        ),
        "affectedLocations": [
            {"file": readme_file, "startLine": readme_line, "endLine": readme_line},
            {"file": package_file, "startLine": package_line, "endLine": package_line},
        ],
        "evidence": [
            {
                "type": "documentation",
                "label": "README command",
                "summary": f"`{readme_file}` documents `{command}`.",
                "file": readme_file,
                "startLine": readme_line,
                "endLine": readme_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "code",
                "label": "package.json scripts",
                "summary": f"The root scripts object does not define `{script}`.",
                "file": package_file,
                "startLine": package_line,
                "endLine": package_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command],
            "input": f"README command in {repo}: `{command}`",
            "expected": f"`{package_file}` defines a `{script}` script or the README uses an existing command.",
            "actual": f"`{package_file}` has no `{script}` script in the root scripts object.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The command is explicitly documented in `{readme_file}`.",
            f"The root `{package_file}` scripts object was parsed as JSON and does not contain `{script}`.",
        ],
        "limitations": [
            "A monorepo package or external wrapper could provide this command outside the root package.json.",
            "The checker does not execute the command, so this is static proof of a documentation/config mismatch.",
        ],
        "file": readme_file,
        "line": readme_line,
        "confidence": 0.9,
        "confidenceRationale": (
            "High-confidence static comparison of README command text and root package.json scripts; production "
            "impact depends on whether users rely on this documented root command."
        ),
        "autoFix": auto_fix,
        "effort": "5 min",
        "fixBenefits": "Keeps documented setup and verification commands aligned with package.json.",
        "fixRisks": "Low; either add the missing script or update the README to the command the project actually supports.",
        "tags": ["deterministic", "static-proof", "docs", "package-json"],
        "steps": [
            f"Decide whether `{command}` should be supported at the repository root.",
            f"Add a `{script}` entry to `{package_file}` or change `{readme_file}` to an existing script.",
            *(
                [f"Preview the generated patch that changes `{command}` to `{replacement_command}` in `{readme_file}`."]
                if auto_fix
                else []
            ),
        ],
        "badCode": bad_code,
        "goodCode": good_code,
        "references": [],
    }


def missing_workflow_package_script_finding(
    *,
    job: dict,
    manager: str,
    script: str,
    workflow_file: str,
    workflow_line: int,
    package_file: str,
    package_line: int,
    replacement_script: str,
    source_line: str,
    replacement_line: str,
) -> dict:
    command = f"{manager} run {script}"
    replacement_command = f"{manager} run {replacement_script}" if replacement_script else ""
    auto_fix = bool(source_line and replacement_line and source_line != replacement_line)
    bad_code = [{"ln": workflow_line, "code": source_line, "t": "del"}] if auto_fix else []
    good_code = [{"ln": workflow_line, "code": replacement_line, "t": "add"}] if auto_fix else []
    severity = "medium" if script in _HIGH_SIGNAL_PACKAGE_SCRIPTS else "low"
    digest_input = f"{workflow_file}:{workflow_line}:{package_file}:{script}".encode("utf-8")
    finding_id = f"static_ci_missing_script_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    commit = str(job.get("commit") or "the scanned commit")
    grep_command = f'git grep -n "{command}" -- {shell_quote(workflow_file)}'
    return {
        "id": finding_id,
        "severity": severity,
        "category": "CI",
        "title": f"GitHub Actions references missing package script `{script}`",
        "summary": (
            f"`{workflow_file}` runs `{command}`, but the root package.json scripts object does not define `{script}`."
        ),
        "impact": (
            "The CI workflow can fail before build or test logic runs, blocking reproducible verification for this commit."
        ),
        "detectionReasoning": (
            f"Static repository check compared `{workflow_file}` with `{package_file}` at commit `{commit}` and found "
            f"no `{script}` script."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{workflow_file}` line {workflow_line} and `{package_file}` line "
            f"{package_line}; then run `{command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The GitHub Actions command and package.json scripts were compared statically; the workflow was not executed."
        ),
        "affectedLocations": [
            {"file": workflow_file, "startLine": workflow_line, "endLine": workflow_line},
            {"file": package_file, "startLine": package_line, "endLine": package_line},
        ],
        "evidence": [
            {
                "type": "tool",
                "label": "GitHub Actions command",
                "summary": f"`{workflow_file}` runs `{command}`.",
                "file": workflow_file,
                "startLine": workflow_line,
                "endLine": workflow_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "code",
                "label": "package.json scripts",
                "summary": f"The root scripts object does not define `{script}`.",
                "file": package_file,
                "startLine": package_line,
                "endLine": package_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command, grep_command],
            "input": f"GitHub Actions command in {workflow_file}: `{command}`",
            "expected": f"`{package_file}` defines a `{script}` script or the workflow uses an existing command.",
            "actual": f"`{package_file}` has no `{script}` script in the root scripts object.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The workflow command is explicitly present in `{workflow_file}`.",
            f"The root `{package_file}` scripts object was parsed as JSON and does not contain `{script}`.",
        ],
        "limitations": [
            "A workflow working-directory or monorepo package may intentionally run this command from another package.",
            "The checker does not execute the workflow, so this is static proof of a CI/package.json mismatch at the repository root.",
        ],
        "file": workflow_file,
        "line": workflow_line,
        "confidence": 0.9,
        "confidenceRationale": (
            "High-confidence static comparison of GitHub Actions command text and root package.json scripts; impact depends on workflow working-directory configuration."
        ),
        "autoFix": auto_fix,
        "effort": "5 min",
        "fixBenefits": "Keeps CI verification commands aligned with package.json so users and automation can reproduce checks.",
        "fixRisks": "Low; either add the missing script or update the workflow to an existing script or explicit working directory.",
        "tags": ["deterministic", "static-proof", "ci", "package-json"],
        "steps": [
            f"Decide whether `{command}` should run at the repository root.",
            f"Add a `{script}` entry to `{package_file}`, update `{workflow_file}`, or set the correct workflow working-directory.",
            *(
                [f"Preview the generated patch that changes `{command}` to `{replacement_command}` in `{workflow_file}`."]
                if auto_fix
                else []
            ),
        ],
        "badCode": bad_code,
        "goodCode": good_code,
        "references": [],
    }


def missing_dockerfile_source_finding(
    *,
    job: dict,
    dockerfile_file: str,
    dockerfile_line: int,
    instruction: str,
    source: str,
) -> dict:
    commit = str(job.get("commit") or "the scanned commit")
    build_command = f"docker build -f {shell_quote(dockerfile_file)} ."
    digest_input = f"{job.get('repo')}:{commit}:{dockerfile_file}:{dockerfile_line}:{source}".encode("utf-8")
    finding_id = f"static_docker_missing_source_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    return {
        "id": finding_id,
        "severity": "medium",
        "category": "Build",
        "title": f"Dockerfile {instruction} source `{source}` is missing",
        "summary": (
            f"`{dockerfile_file}` line {dockerfile_line} uses `{instruction} {source}`, but `{source}` was not found in the repository checkout."
        ),
        "impact": (
            "A clean Docker build can fail before the application starts, blocking a reproducible environment for this commit."
        ),
        "detectionReasoning": (
            f"A deterministic Dockerfile check inspected `{dockerfile_file}` at commit `{commit}` and found a literal "
            f"repository-local `{instruction}` source path that does not exist."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{dockerfile_file}` line {dockerfile_line} and verify that `{source}` exists; "
            f"then run `{build_command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The Dockerfile source path was checked against the repository tree; docker build was not executed."
        ),
        "affectedLocations": [{"file": dockerfile_file, "startLine": dockerfile_line, "endLine": dockerfile_line}],
        "evidence": [
            {
                "type": "code",
                "label": "Dockerfile copy source",
                "summary": f"`{instruction}` references missing repository path `{source}`.",
                "file": dockerfile_file,
                "startLine": dockerfile_line,
                "endLine": dockerfile_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "tool",
                "label": "Repository path check",
                "summary": f"The scanner looked for `{source}` in the repository root and did not find it.",
                "file": dockerfile_file,
                "startLine": dockerfile_line,
                "endLine": dockerfile_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [build_command],
            "input": f"Dockerfile instruction `{instruction} {source}`",
            "expected": f"`{source}` exists in the Docker build context.",
            "actual": f"`{source}` is absent from the repository checkout.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The source path `{source}` is a literal local path, not a URL, glob, variable, or multi-stage `--from` source.",
            f"The finding points to a concrete Dockerfile line: `{dockerfile_file}:{dockerfile_line}`.",
        ],
        "limitations": [
            "The check assumes `docker build` uses the repository root as build context.",
            "If the missing source is generated before docker build, this may be an environment/setup requirement rather than a committed file issue.",
        ],
        "file": dockerfile_file,
        "line": dockerfile_line,
        "confidence": 0.88,
        "confidenceRationale": (
            "A literal Dockerfile COPY/ADD source is missing from the repository tree; impact depends on build context and pre-build generation steps."
        ),
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Restores a Docker build path that users can reproduce from a clean checkout.",
        "fixRisks": "Low; add the missing file, correct the Dockerfile path, or document the required generation step.",
        "tags": ["deterministic", "static-proof", "dockerfile", "build"],
        "steps": [
            f"Confirm whether `{source}` should be committed or generated before Docker build.",
            f"Update `{dockerfile_file}` or add the missing source, then run `{build_command}`.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }
