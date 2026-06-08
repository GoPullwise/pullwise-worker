from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

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
        "providerChain": list(config.provider_chain),
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
            "Runtime verification requires a later sandboxed verifier stage with project dependencies available.",
        ],
    }


class RepositoryTooLargeError(RuntimeError):
    def __init__(self, message: str, preflight: dict) -> None:
        super().__init__(message)
        self.error_code = REPOSITORY_TOO_LARGE_ERROR_CODE
        self.preflight = preflight


def repository_limits_metadata(config: WorkerConfig) -> dict:
    return {
        "maxFiles": max(1, int(getattr(config, "max_repo_files", _DEFAULT_MAX_REPO_FILES) or _DEFAULT_MAX_REPO_FILES)),
        "maxBytes": max(1, int(getattr(config, "max_repo_bytes", _DEFAULT_MAX_REPO_BYTES) or _DEFAULT_MAX_REPO_BYTES)),
    }


def repository_resource_stats(checkout_dir: Path, limits: dict | None = None) -> dict:
    file_count = 0
    total_bytes = 0
    if not checkout_dir.is_dir():
        return {"fileCount": 0, "totalBytes": 0}
    max_files = int(limits.get("maxFiles") or 0) if isinstance(limits, dict) else 0
    max_bytes = int(limits.get("maxBytes") or 0) if isinstance(limits, dict) else 0
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
    if int(stats.get("fileCount") or 0) > int(limits.get("maxFiles") or 0):
        exceeded.append("file_count")
    if int(stats.get("totalBytes") or 0) > int(limits.get("maxBytes") or 0):
        exceeded.append("total_bytes")
    return exceeded


def repository_limit_preflight_metadata(config: WorkerConfig, job: dict, checkout_dir: Path) -> dict:
    limits = repository_limits_metadata(config)
    stats = repository_resource_stats(checkout_dir, limits=limits)
    exceeded = repository_limit_exceeded(stats, limits)
    summary = (
        "Repository checkout exceeds Pullwise worker repository limits; verifier and AI review were not executed."
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
        "providerChain": list(config.provider_chain),
        "repositoryStats": stats,
        "repositoryLimits": limits,
        "repositoryLimitExceeded": bool(exceeded),
        "repositoryLimitReasons": exceeded,
        "limitations": [
            "Dependency installation, verifier commands, and AI review were not executed before this repository size check.",
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
        if path.is_file():
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manifest_type in sorted(_CONFIG_MANIFEST_TYPES.items()):
        path = checkout_dir / filename
        if path.is_file():
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        path = checkout_dir / filename
        if path.is_file():
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
        if path.is_file():
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
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def package_managers_for_repository(checkout_dir: Path, package_json: dict) -> list[str]:
    managers = []
    package_manager = str(package_json.get("packageManager") or "").strip()
    if package_manager:
        managers.append(package_manager.split("@", 1)[0])
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        if (checkout_dir / filename).is_file():
            managers.append(manager)
    if (checkout_dir / "package.json").is_file() and not managers:
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
    if "codex" in config.provider_chain:
        checks.append(("codex", [config.codex_command, "--version"]))
    if "opencode" in config.provider_chain:
        checks.append(("opencode", [config.opencode_command, "--version"]))
    return [safe_tool_version(name, command) for name, command in checks]


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


def safe_tool_version(name: str, command: list[str]) -> dict:
    command_text = public_tool_version_command(command)
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": name,
            "command": command_text,
            "available": False,
            "exitCode": 127,
            "output": str(exc)[:200],
        }
    output = " ".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    return {
        "name": name,
        "command": command_text,
        "available": completed.returncode == 0,
        "exitCode": completed.returncode,
        "output": output[:200],
    }


def run_verifier_commands(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    preflight: dict,
) -> tuple[dict, list[dict], str]:
    if not config.verifier_enabled:
        return (
            {
                "enabled": False,
                "summary": "Verifier command execution is disabled for this worker.",
                "runs": [],
            },
            [],
            "verifier disabled",
        )

    if not config.verifier_host_execution_allowed:
        return (
            {
                "enabled": True,
                "summary": (
                    "Verifier command execution is enabled, but host execution is not allowed. "
                    "Run verifier commands only inside an external sandbox or set "
                    "PULLWISE_WORKER_VERIFIER_ALLOW_HOST_EXECUTION=true for trusted hosts."
                ),
                "runs": [],
            },
            [],
            "verifier host execution disabled",
        )

    package_managers = preflight.get("packageManagers") if isinstance(preflight.get("packageManagers"), list) else []
    package_manager = str(package_managers[0] if package_managers else "npm")
    available_scripts = preflight.get("availableScripts") if isinstance(preflight.get("availableScripts"), list) else []
    scripts = [script for script in config.verifier_scripts if script in available_scripts][: config.verifier_max_commands]
    install_command = package_install_command(package_manager, checkout_dir) if config.verifier_install_deps else []
    if not scripts and not install_command:
        return (
            {
                "enabled": True,
                "summary": "Verifier enabled, but no dependency install command or allowlisted package scripts were present.",
                "runs": [],
            },
            [],
            "verifier no runnable scripts",
        )

    runs = []
    if install_command:
        install_run = execute_verifier_command(
            config,
            job,
            checkout_dir,
            "install-deps",
            install_command,
        )
        runs.append(install_run)
        if install_run.get("status") not in {"passed", "flaky"}:
            findings = verifier_findings_from_results(job, checkout_dir, runs)
            summary = verifier_runs_summary(runs)
            return (
                {
                    "enabled": True,
                    "summary": summary,
                    "runs": runs,
                },
                findings,
                summary,
            )
    runs.extend(
        execute_verifier_script(config, job, checkout_dir, package_manager, script)
        for script in scripts
    )
    findings = verifier_findings_from_results(job, checkout_dir, runs)
    summary = verifier_runs_summary(runs)
    return (
        {
            "enabled": True,
            "summary": summary,
            "runs": runs,
        },
        findings,
        summary,
    )


def verifier_runs_summary(runs: list[dict]) -> str:
    failed_count = len([run for run in runs if run.get("status") == "failed"])
    flaky_count = len([run for run in runs if run.get("status") == "flaky"])
    passed_count = len([run for run in runs if run.get("status") == "passed"])
    skipped_count = len([run for run in runs if run.get("status") in {"skipped", "timeout"}])
    return (
        f"Verifier ran {len(runs)} allowlisted command(s): {passed_count} passed, "
        f"{failed_count} failed, {flaky_count} flaky, {skipped_count} skipped or timed out."
    )


def execute_verifier_script(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    package_manager: str,
    script: str,
) -> dict:
    command = package_script_command(package_manager, script)
    return execute_verifier_command(config, job, checkout_dir, script, command)


def execute_verifier_command(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    label: str,
    command: list[str],
) -> dict:
    started = time.monotonic()
    log_path, public_log_path = verifier_log_path(config, job, label)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = verifier_command_env(checkout_dir)
    attempts = [execute_verifier_attempt(config, checkout_dir, command, env, 1)]
    if attempts[0].get("status") == "failed" and config.verifier_confirm_failures:
        attempts.append(execute_verifier_attempt(config, checkout_dir, command, env, 2))

    confirmed_failure = attempts[0].get("status") == "failed" and (
        not config.verifier_confirm_failures
        or (len(attempts) > 1 and attempts[1].get("status") == "failed")
    )
    if confirmed_failure:
        status = "failed"
        result_attempt = attempts[-1]
    elif attempts[0].get("status") == "failed":
        status = "flaky"
        result_attempt = attempts[0]
    else:
        status = str(attempts[0].get("status") or "skipped")
        result_attempt = attempts[0]

    output = verifier_attempts_output(attempts)
    log_path.write_text(output, encoding="utf-8")
    public_attempts = [verifier_public_attempt(attempt) for attempt in attempts]
    return {
        "script": label,
        "command": " ".join(command),
        "status": status,
        "exitCode": int(result_attempt.get("exitCode") or 0),
        "durationMs": int((time.monotonic() - started) * 1000),
        "logPath": public_log_path,
        "outputRedacted": bool(output),
        "outputSummary": _VERIFIER_OUTPUT_WITHHELD if output else "",
        "attempts": public_attempts,
        "confirmedFailure": bool(confirmed_failure),
    }


def verifier_command_env(checkout_dir: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _VERIFIER_ENV_PASSTHROUGH_KEYS:
        if key in os.environ and os.environ[key]:
            env[key] = os.environ[key]
    sandbox_root = checkout_dir.parent
    home_dir = sandbox_root / _VERIFIER_HOME_DIR_NAME
    tmp_dir = sandbox_root / _VERIFIER_TMP_DIR_NAME
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CI": "true",
            "PULLWISE_VERIFY": "1",
            "NO_COLOR": "1",
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "TMPDIR": str(tmp_dir),
            "TMP": str(tmp_dir),
            "TEMP": str(tmp_dir),
        }
    )
    return env


def verifier_public_attempt(attempt: dict) -> dict:
    public = {
        "attempt": attempt.get("attempt"),
        "status": attempt.get("status"),
        "exitCode": attempt.get("exitCode"),
        "durationMs": attempt.get("durationMs"),
    }
    if attempt.get("output"):
        public["outputRedacted"] = True
        public["outputSummary"] = _VERIFIER_OUTPUT_WITHHELD
    return public


def execute_verifier_attempt(
    config: WorkerConfig,
    checkout_dir: Path,
    command: list[str],
    env: dict[str, str],
    attempt: int,
) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(checkout_dir),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.verifier_timeout_seconds,
            env=env,
        )
        output = verifier_output_text(completed.stdout, completed.stderr)
        status = "passed" if completed.returncode == 0 else "failed"
        return {
            "attempt": attempt,
            "status": status,
            "exitCode": completed.returncode,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output[-_VERIFIER_MAX_OUTPUT_CHARS:],
        }
    except FileNotFoundError as exc:
        output = str(exc)
        return {
            "attempt": attempt,
            "status": "skipped",
            "exitCode": 127,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        output = verifier_output_text(exc.stdout, exc.stderr) or f"Command timed out after {config.verifier_timeout_seconds}s."
        return {
            "attempt": attempt,
            "status": "timeout",
            "exitCode": 124,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output[-_VERIFIER_MAX_OUTPUT_CHARS:],
        }


def verifier_attempts_output(attempts: list[dict]) -> str:
    if len(attempts) == 1:
        return str(attempts[0].get("output") or "")[-_VERIFIER_MAX_OUTPUT_CHARS:]
    parts = []
    for attempt in attempts:
        parts.append(
            f"--- attempt {attempt.get('attempt')} "
            f"({attempt.get('status')} exit {attempt.get('exitCode')}) ---"
        )
        output = str(attempt.get("output") or "").rstrip()
        if output:
            parts.append(output)
    return "\n".join(parts)[-_VERIFIER_MAX_OUTPUT_CHARS:]


def package_script_command(package_manager: str, script: str) -> list[str]:
    manager = package_manager if package_manager in {"npm", "pnpm", "yarn", "bun"} else "npm"
    return [manager, "run", script]


def package_install_command(package_manager: str, checkout_dir: Path) -> list[str]:
    if not (checkout_dir / "package.json").is_file():
        return []
    manager = package_manager if package_manager in {"npm", "pnpm", "yarn", "bun"} else "npm"
    if manager == "npm":
        command = ["npm", "ci"] if (checkout_dir / "package-lock.json").is_file() else ["npm", "install"]
        return [*command, "--ignore-scripts"]
    if manager == "pnpm":
        command = ["pnpm", "install"]
        if (checkout_dir / "pnpm-lock.yaml").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    if manager == "yarn":
        command = ["yarn", "install"]
        if (checkout_dir / "yarn.lock").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    if manager == "bun":
        command = ["bun", "install"]
        if (checkout_dir / "bun.lock").is_file() or (checkout_dir / "bun.lockb").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    return []


def verifier_log_path(config: WorkerConfig, job: dict, script: str) -> tuple[Path, str]:
    job_id = safe_job_id(job.get("job_id") or job.get("scan_id") or "scan")
    safe_script = re.sub(r"[^A-Za-z0-9_.-]+", "_", script).strip("._") or "script"
    relative = Path("verification") / job_id / f"{safe_script}.log"
    return config.log_dir / relative, relative.as_posix()


def verifier_output_text(stdout: object, stderr: object) -> str:
    parts = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value:
            parts.append(value)
    output = "\n".join(parts).replace("\x00", "")
    return output[-_VERIFIER_MAX_OUTPUT_CHARS:]


def verifier_findings_from_results(job: dict, checkout_dir: Path, runs: list[dict]) -> list[dict]:
    findings = []
    for run in runs:
        if run.get("status") != "failed":
            continue
        if run.get("confirmedFailure") is False:
            continue
        findings.append(verifier_command_failure_finding(job, checkout_dir, run))
    return findings


def verifier_command_failure_finding(job: dict, checkout_dir: Path, run: dict) -> dict:
    script = str(run.get("script") or "script")
    command = str(run.get("command") or "")
    exit_code = int(run.get("exitCode") or 1)
    log_path = str(run.get("logPath") or "")
    is_install = script == "install-deps"
    location_file = install_evidence_file(checkout_dir) if is_install else "package.json"
    line = 1 if is_install else package_script_line(checkout_dir, script)
    title = f"`{command}` fails during dependency installation" if is_install else f"`{command}` fails in verifier"
    severity = "high" if script in {"build", "test", "typecheck", "check", "install-deps"} else "medium"
    category = "Dependencies" if is_install else ("Tests" if script == "test" else "Quality")
    digest_input = f"{job.get('repo')}:{job.get('commit')}:{script}:{exit_code}:{log_path}".encode("utf-8")
    finding_id = f"verified_command_failure_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    code_label = "dependency manifest" if is_install else "package.json script"
    code_summary = (
        f"The verifier installs dependencies from `{location_file}` before running project checks."
        if is_install
        else f"The failing verifier command maps to the `{script}` script in package.json."
    )
    attempts = run.get("attempts") if isinstance(run.get("attempts"), list) else []
    attempt_count = len(attempts) or 1
    confirmed_text = " on two consecutive attempts" if attempt_count > 1 else ""
    impact = (
        "The project dependencies could not be installed in the verifier environment, so build/test reproduction is blocked before application checks run."
        if is_install
        else "The documented or standard project quality gate currently fails in the verifier environment."
    )
    limitation = (
        "Private registries, missing package manager credentials, or network restrictions can make dependency installation fail outside production."
        if is_install
        else "Private services or environment variables absent from the worker can make this fail outside production."
    )
    confidence_rationale = (
        "The command failure is directly observed and confirmed by a repeated verifier attempt; "
        "production impact depends on environment parity."
        if attempt_count > 1
        else "The command failure is directly observed; production impact depends on environment parity."
    )
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title[:120],
        "summary": f"The verifier ran `{command}` and it exited with code {exit_code}{confirmed_text}.",
        "impact": impact,
        "detectionReasoning": (
            "This finding comes from an executed allowlisted verifier command, not from model inference. "
            f"The command returned exit code {exit_code}{confirmed_text}; stdout/stderr is retained only in the worker-local log."
        ),
        "reproductionPath": f"At commit `{job.get('commit') or 'pending'}`, run `{command}` from the repository root.",
        "verificationStatus": "verified",
        "verificationSummary": (
            f"`{command}` was executed by the worker verifier and failed with exit code {exit_code}{confirmed_text}."
        ),
        "affectedLocations": [{"file": location_file, "startLine": line, "endLine": line}],
        "evidence": [
            {
                "type": "runtime_log",
                "label": "Verifier command output",
                "summary": f"`{command}` exited {exit_code}. Raw stdout/stderr is withheld from API and audit bundle payloads.",
                "file": "",
                "startLine": 0,
                "endLine": 0,
                "command": command,
                "exitCode": exit_code,
                "logPath": log_path,
                "outputRedacted": True,
                "url": "",
            },
            {
                "type": "code",
                "label": code_label,
                "summary": code_summary,
                "file": location_file,
                "startLine": line,
                "endLine": line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command],
            "input": f"Run `{command}` at repository root.",
            "expected": "Command exits 0.",
            "actual": f"Command exited {exit_code}; stdout/stderr is withheld from shared payloads.",
            "testFile": "",
            "logPath": log_path,
        },
        "whyNotFalsePositive": [
            (
                "The command was executed by the worker verifier and returned a non-zero exit code on two consecutive attempts."
                if attempt_count > 1
                else "The command was executed by the worker verifier and returned a non-zero exit code."
            ),
            (
                "Dependency installation is an explicit verifier setup step before running project checks."
                if is_install
                else "Only allowlisted project scripts are promoted to verifier findings."
            ),
        ],
        "limitations": [
            (
                "Dependency installation uses an allowlisted package-manager command with install scripts disabled."
                if is_install
                else "The verifier runs only allowlisted project scripts after any configured setup step."
            ),
            limitation,
        ],
        "file": location_file,
        "line": line,
        "confidence": 0.95,
        "confidenceRationale": confidence_rationale,
        "autoFix": False,
        "effort": "review required",
        "fixBenefits": (
            "Restores dependency installation so build and test reproduction can run from a clean checkout."
            if is_install
            else "Restores a failing quality gate and gives users a copyable command to verify the fix."
        ),
        "fixRisks": "The root cause may be missing verifier environment configuration rather than application code.",
        "tags": ["verified", "verifier", "command-failure", script],
        "steps": [
            f"Run `{command}` locally at the pinned commit.",
            "Inspect the captured output and fix the first failing error.",
            "Rerun the same command and the existing test suite.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def package_script_line(checkout_dir: Path, script: str) -> int:
    package_path = checkout_dir / "package.json"
    if not package_path.is_file():
        return 1
    try:
        package_text = package_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 1
    return first_matching_line(package_text, rf'"{re.escape(script)}"\s*:')


def install_evidence_file(checkout_dir: Path) -> str:
    for filename in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"):
        if (checkout_dir / filename).is_file():
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
    if not package_path.is_file():
        return []
    readme_path = first_existing_file(checkout_dir, ["README.md", "README.markdown", "README"])
    if readme_path is None:
        return []

    try:
        package_text = package_path.read_text(encoding="utf-8")
        package_data = json.loads(package_text)
        readme_text = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
            findings.append(
                missing_package_script_finding(
                    job=job,
                    manager=manager,
                    script=script,
                    readme_file=readme_rel,
                    readme_line=line_number,
                    package_file=package_rel,
                    package_line=scripts_line,
                )
            )
    return findings


def workflow_missing_package_script_findings(job: dict, checkout_dir: Path) -> list[dict]:
    package_path = checkout_dir / "package.json"
    workflows_dir = checkout_dir / ".github" / "workflows"
    if not package_path.is_file() or not workflows_dir.is_dir():
        return []

    try:
        package_text = package_path.read_text(encoding="utf-8")
        package_data = json.loads(package_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    defined_scripts = {str(name) for name in scripts if isinstance(name, str)} if isinstance(scripts, dict) else set()
    package_rel = "package.json"
    scripts_line = first_matching_line(package_text, r'"scripts"\s*:') if isinstance(scripts, dict) else 1
    seen: set[tuple[str, str]] = set()
    findings: list[dict] = []
    for workflow_path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        try:
            workflow_text = workflow_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
                findings.append(
                    missing_workflow_package_script_finding(
                        job=job,
                        manager=manager,
                        script=script,
                        workflow_file=workflow_rel,
                        workflow_line=line_number,
                        package_file=package_rel,
                        package_line=scripts_line,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def dockerfile_missing_source_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for dockerfile_path in iter_dockerfiles(checkout_dir):
        try:
            dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
    candidates = []
    root_dockerfile = checkout_dir / "Dockerfile"
    if root_dockerfile.is_file():
        candidates.append(root_dockerfile)
    candidates.extend(path for path in checkout_dir.rglob("Dockerfile") if path.is_file() and path != root_dockerfile)
    candidates.extend(path for path in checkout_dir.rglob("*.Dockerfile") if path.is_file())
    yielded = 0
    seen = set()
    for path in sorted(candidates):
        if yielded >= _DOCKERFILE_SCAN_MAX_FILES:
            return
        if path in seen or not dockerfile_scan_allowed(path, checkout_dir):
            continue
        seen.add(path)
        yielded += 1
        yield path


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
    return target.exists()


def first_existing_file(root: Path, names: list[str]) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def first_matching_line(text: str, pattern: str) -> int:
    compiled = re.compile(pattern)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if compiled.search(line):
            return line_number
    return 1


def committed_secret_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen_locations: set[tuple[str, str]] = set()
    for path in iter_secret_scan_files(checkout_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
    if not path.is_file():
        return False
    try:
        relative = path.relative_to(checkout_dir)
        size = path.stat().st_size
    except (OSError, ValueError):
        return False
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
) -> dict:
    command = f"{manager} run {script}"
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
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Keeps documented setup and verification commands aligned with package.json.",
        "fixRisks": "Low; either add the missing script or update the README to the command the project actually supports.",
        "tags": ["deterministic", "static-proof", "docs", "package-json"],
        "steps": [
            f"Decide whether `{command}` should be supported at the repository root.",
            f"Add a `{script}` entry to `{package_file}` or change `{readme_file}` to an existing script.",
        ],
        "badCode": [],
        "goodCode": [],
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
) -> dict:
    command = f"{manager} run {script}"
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
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Keeps CI verification commands aligned with package.json so users and automation can reproduce checks.",
        "fixRisks": "Low; either add the missing script or update the workflow to an existing script or explicit working directory.",
        "tags": ["deterministic", "static-proof", "ci", "package-json"],
        "steps": [
            f"Decide whether `{command}` should run at the repository root.",
            f"Add a `{script}` entry to `{package_file}`, update `{workflow_file}`, or set the correct workflow working-directory.",
        ],
        "badCode": [],
        "goodCode": [],
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


