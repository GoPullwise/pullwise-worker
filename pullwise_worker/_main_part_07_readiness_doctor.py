from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

import posixpath

def result_checksum(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def redact_secrets(text: str, config: WorkerConfig | None = None) -> str:
    redacted = str(text or "")
    if config and config.worker_token:
        redacted = redacted.replace(config.worker_token, "[redacted]")
    redacted = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:[redacted]@", redacted)
    return redacted


def write_scan_summary(
    config: WorkerConfig,
    job_id: str,
    status: str,
    duration_ms: int,
    error: str,
    review_execution: dict | None = None,
) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    path = config.log_dir / "scan-summary.log"
    payload = {
        "time": int(time.time()),
        "job_id": job_id,
        "status": status,
        "duration_ms": duration_ms,
        "error": redact_secrets(error, config),
    }
    if isinstance(review_execution, dict) and review_execution:
        provider = clean_protocol_text(review_execution.get("provider"))
        if provider:
            payload["review_provider"] = provider
        payload["codex_queue_wait_ms"] = nonnegative_int(review_execution.get("queueWaitMs"))
        payload["codex_exec_duration_ms"] = nonnegative_int(review_execution.get("execDurationMs"))
        payload["codex_timeout_seconds"] = nonnegative_int(review_execution.get("timeoutSeconds"))
    line = json.dumps(
        payload,
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")
    trim_file_to_last_bytes(path, config.scan_summary_log_max_bytes)


def provider_command_scope_check(command: str, config: WorkerConfig, label: str) -> tuple[bool, str]:
    raw = str(command or "").strip()
    if not raw:
        return False, f"{label} command missing"
    home_raw = str(config.service_home or "").strip()
    if os.name == "nt" and raw.startswith("/") and home_raw.startswith("/"):
        resolved_home = posixpath.normpath(home_raw)
        resolved_command = posixpath.normpath(raw)
        if not PurePosixPath(resolved_command).is_absolute():
            return False, f"{label} command must be an absolute path inside worker home {config.service_home}: {raw}"
        if resolved_command == resolved_home or resolved_command.startswith(f"{resolved_home}/"):
            return True, resolved_command
        return False, f"{label} command outside worker home {resolved_home}: {raw}"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return False, f"{label} command must be an absolute path inside worker home {config.service_home}: {raw}"
    try:
        home = Path(config.service_home).expanduser().resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(home)
    except ValueError:
        return False, f"{label} command outside worker home {home}: {raw}"
    except Exception as exc:
        return False, str(exc)
    return True, str(resolved)


def worker_provider_home_isolation_check(config: WorkerConfig) -> tuple[bool, str]:
    service_home = str(config.service_home or "").strip()
    if not service_home:
        return False, "PULLWISE_SERVICE_HOME is required"
    normalized = service_home.replace("\\", "/").rstrip("/")
    default_home = DEFAULT_SERVICE_HOME.rstrip("/")
    if normalized == default_home:
        return (
            False,
            f"PULLWISE_SERVICE_HOME must be worker-instance-specific, not shared default {DEFAULT_SERVICE_HOME}",
        )
    return True, service_home


def worker_readiness_state(config: WorkerConfig) -> tuple[list[tuple[str, bool, str]], bool, list[str]]:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("server_url", server_url_allowed(config.server_url, allow_insecure=config.allow_insecure_server_url), config.server_url))
    checks.append(("worker_token", bool(config.worker_token), "configured" if config.worker_token else "missing"))
    agent_configs_ok, agent_configs_detail, agent_configs = worker_agent_configs_check(config)
    checks.append(("agent_configs", agent_configs_ok, agent_configs_detail))
    checks.append(("max_concurrent_jobs", config.max_concurrent_jobs > 0, str(config.max_concurrent_jobs)))

    provider_env = provider_process_env(config)
    git_ok, git_detail = command_ok(["git", "--version"])
    checks.append(("git", git_ok, git_detail))
    ready_providers: list[str] = []
    required_providers = subscription_plan_required_providers(agent_configs) if agent_configs_ok else []
    local_provider_chain = parse_provider_chain(",".join(config.provider_chain))
    providers_to_check = [
        provider
        for provider in required_providers
        if not local_provider_chain or provider in local_provider_chain
    ]
    skipped_required = [provider for provider in required_providers if provider not in providers_to_check]
    if skipped_required:
        checks.append(
            (
                "provider_capability",
                True,
                f"skipped providers outside PULLWISE_PROVIDER_CHAIN: {', '.join(skipped_required)}",
            )
        )
    if "codex" in providers_to_check:
        home_ok, home_detail = worker_provider_home_isolation_check(config)
        checks.append(("worker_home_isolation", home_ok, home_detail))
        node_ok, node_detail = node_version_check(env=provider_env) if home_ok else (False, "skipped until worker home isolation passes")
        checks.append(("node", node_ok, node_detail))
        codex_scope_ok, codex_scope_detail = (
            provider_command_scope_check(config.codex_command, config, "Codex")
            if home_ok
            else (False, "skipped until worker home isolation passes")
        )
        codex_cli_ok, codex_cli_detail = (
            command_ok([config.codex_command, "--version"], env=provider_env) if codex_scope_ok else (False, codex_scope_detail)
        )
        checks.append(("codex", codex_cli_ok, codex_cli_detail))
        codex_login_ok, codex_login_detail = codex_ready_check(config) if codex_cli_ok else (False, "skipped until codex CLI passes --version")
        if (
            not codex_login_ok
            and "deferred" in str(codex_login_detail or "").lower()
            and "codex is running" in str(codex_login_detail or "").lower()
        ):
            codex_login_ok = True
            codex_login_detail = "busy: ready check deferred while codex is running"
        checks.append(("codex_ready", codex_login_ok, codex_login_detail))
        if node_ok and codex_cli_ok and codex_login_ok:
            ready_providers.append("codex")
    provider_ready = bool(ready_providers)
    provider_ready_detail = (
        ", ".join(ready_providers)
        if provider_ready
        else "no locally configured provider matches subscription plan agent configs"
        if agent_configs_ok and required_providers and not providers_to_check
        else "no configured provider is ready" if agent_configs_ok else "subscription plan agent configs unavailable"
    )
    checks.append(("provider_ready", provider_ready, provider_ready_detail))

    for label, path in (("checkout_root", config.work_dir), ("log_dir", config.log_dir)):
        ok, detail = writable_path_check(path)
        checks.append((label, ok, detail))
    checks.append(("disk_space", *disk_space_check(config.work_dir)))
    return checks, provider_ready, ready_providers


def worker_readiness_checks(config: WorkerConfig) -> tuple[list[tuple[str, bool, str]], bool]:
    checks, provider_ready, _ready_providers = worker_readiness_state(config)
    return checks, provider_ready


def first_failed_check(checks: list[tuple[str, bool, str]]) -> tuple[str, bool, str] | None:
    provider_ready = readiness_check_ok(checks, "provider_ready")
    optional_provider_checks = {"node", "codex", "codex_ready"}
    for check in checks:
        name, ok, _detail = check
        if ok:
            continue
        if provider_ready and name in optional_provider_checks:
            continue
        return check
    return None


def readiness_check_ok(checks: list[tuple[str, bool, str]], name: str) -> bool:
    return any(check_name == name and ok for check_name, ok, _detail in checks)


def readiness_error_message(check: tuple[str, bool, str], config: WorkerConfig) -> str:
    name, _ok, detail = check
    return f"worker not ready: {name}: {redact_secrets(detail, config)}"[:500]


def writable_path_check(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / f".pullwise-write-test-{os.getpid()}"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def disk_space_check(path: Path) -> tuple[bool, str]:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except Exception as exc:
        return False, str(exc)
    return usage.free > _MIN_READY_DISK_BYTES, f"{usage.free // (1024 * 1024)} MB free"


def run_doctor(config: WorkerConfig) -> bool:
    requirements = ["git"]
    local_provider_chain = parse_provider_chain(",".join(config.provider_chain) if config.provider_chain else None, config.provider)
    if "codex" in local_provider_chain:
        requirements.extend(["node", "npm"])
    dependency_ok, dependency_detail = install_ubuntu_2204_dependencies(requirements)
    checks, _provider_ready, ready_providers = worker_readiness_state(config)
    if not dependency_ok or dependency_detail != "dependencies present":
        checks.insert(0, ("dependency_install", dependency_ok, dependency_detail))
    codex_ready = readiness_check_ok(checks, "codex_ready")
    systemd_ok, systemd_detail = command_ok(["systemctl", "is-active", config.service_name])
    checks.append(("systemd", systemd_ok, systemd_detail))
    heartbeat_ok = True
    heartbeat_detail = "ok"
    doctor_required_ok = first_failed_check(checks) is None
    try:
        PullwiseClient(config).heartbeat(
            last_error=None,
            doctor_status="ok" if doctor_required_ok else "degraded",
            codex_ready=codex_ready,
            ready_providers=ready_providers,
            systemd_active=systemd_ok,
            doctor_checked_at=int(time.time()),
        )
    except Exception as exc:
        heartbeat_ok = False
        heartbeat_detail = redact_secrets(str(exc), config)
    checks.append(("heartbeat", heartbeat_ok, heartbeat_detail))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'fail'} {name}: {detail}")
    codex_login_check = next((check for check in checks if check[0] == "codex_ready"), None)
    if codex_login_check and not codex_login_check[1]:
        print(f"Codex may require device authorization. Run: {codex_login_command(config)}")
    return first_failed_check(checks) is None


def command_ok(command: list[str], *, env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        detail = (completed.stdout or completed.stderr).strip().splitlines()
        return completed.returncode == 0, detail[0] if detail else f"exit {completed.returncode}"
    except FileNotFoundError:
        return False, "not found"
    except Exception as exc:
        return False, str(exc)



def worker_agent_configs_check(config: WorkerConfig) -> tuple[bool, str, dict | None]:
    try:
        payload = PullwiseClient(config).agent_configs()
    except Exception as exc:
        return False, f"unable to load subscription plan agent configs: {redact_secrets(str(exc), config)}", None
    plan_configs = subscription_plan_agent_configs(payload)
    missing_plans = [plan for plan in ("free", "pro", "max") if plan not in plan_configs]
    if missing_plans:
        return False, f"subscription plan agent configs missing: {', '.join(missing_plans)}", payload
    validation_error = subscription_plan_agent_configs_validation_error(plan_configs)
    if validation_error:
        return False, validation_error, payload
    return True, "loaded free/pro/max subscription plan agent configs", payload


def subscription_plan_agent_configs(payload: dict | None) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    raw_configs = payload.get("agentConfigs")
    if not isinstance(raw_configs, dict):
        return {}
    configs: dict[str, dict] = {}
    for plan in ("free", "pro", "max"):
        plan_config = raw_configs.get(plan)
        if isinstance(plan_config, dict):
            configs[plan] = plan_config
    return configs


def subscription_plan_agent_configs_validation_error(plan_configs: dict[str, dict]) -> str:
    for plan in ("free", "pro", "max"):
        agent_config = plan_configs.get(plan) or {}
        provider = agent_config_provider(agent_config)
        if not provider:
            return f"subscription plan agent configs invalid: {plan}.provider is required"
        if provider == "codex":
            codex_config = agent_config.get("codex") if isinstance(agent_config.get("codex"), dict) else {}
            if not normalized_agent_config_text(codex_config.get("model")):
                return f"subscription plan agent configs invalid: {plan}.codex.model is required"
            if not normalized_agent_reasoning_level(codex_config.get("reasoningEffort")):
                return f"subscription plan agent configs invalid: {plan}.codex.reasoningEffort is required"
    return ""


def agent_config_provider(agent_config: dict) -> str:
    provider = str(agent_config.get("provider") if isinstance(agent_config, dict) else "").strip().lower()
    return provider if provider in SUPPORTED_REVIEW_PROVIDERS else ""


def subscription_plan_required_providers(payload: dict | None) -> list[str]:
    required: list[str] = []
    for agent_config in subscription_plan_agent_configs(payload).values():
        provider = agent_config_provider(agent_config)
        if provider and provider not in required:
            required.append(provider)
    return required



def node_version_check(*, env: dict[str, str] | None = None) -> tuple[bool, str]:
    ok, detail = command_ok(["node", "--version"], env=env)
    if not ok:
        return False, detail
    match = re.search(r"v?(\d+)", detail.strip())
    if not match:
        return False, f"unable to parse Node.js version: {detail}"
    if int(match.group(1)) < _MIN_NODE_MAJOR:
        return False, f"Node.js {_MIN_NODE_MAJOR}+ required, found {detail}"
    return True, detail


def codex_node_runtime_error(output: str) -> bool:
    lowered = output.lower()
    return "@openai/codex" in lowered and "syntaxerror: unexpected reserved word" in lowered


def codex_ready_probe_confirmed(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    for candidate in [raw, *raw.splitlines()]:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("ok") is True:
            return True
    return False


def codex_ready_check(config: WorkerConfig) -> tuple[bool, str]:
    if not _CODEX_EXEC_LOCK.acquire(blocking=False):
        return False, "ready check deferred while codex is running"
    try:
        scope_ok, scope_detail = provider_command_scope_check(config.codex_command, config, "Codex")
        if not scope_ok:
            return False, scope_detail
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "codex-ready.json"
            command = [
                config.codex_command,
                "--ask-for-approval",
                "never",
                "exec",
                _CODEX_SKIP_GIT_REPO_CHECK_ARG,
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--json",
                "--output-last-message",
                str(output_path),
                "--config",
                f'model_reasoning_effort="{config.codex_reasoning_effort}"',
                "--sandbox",
                "read-only",
            ]
            if config.codex_model:
                command.extend(["--model", config.codex_model])
            command.append('Return only JSON: {"ok": true}')
            try:
                completed = subprocess.run(
                    command,
                    env=provider_process_env(config),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=config.codex_doctor_timeout_seconds,
                )
            except FileNotFoundError:
                return False, "codex not found"
            except subprocess.TimeoutExpired:
                return False, "codex ready check timed out"
            except Exception as exc:
                return False, str(exc)
            output = redact_secrets((completed.stderr or completed.stdout).strip(), config)
            detail = output.splitlines()[0] if output else f"exit {completed.returncode}"
            if completed.returncode == 0:
                final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
                if not (codex_ready_probe_confirmed(final_message) or codex_ready_probe_confirmed(completed.stdout)):
                    return False, "codex ready check did not confirm model response"
                clear_codex_auth_failure()
                return True, "ready"
            if looks_like_codex_auth_failure(output):
                mark_codex_auth_failure(config, output)
            lowered = output.lower()
            if "login" in lowered or "auth" in lowered or "api key" in lowered or "not authenticated" in lowered:
                return False, "not logged in"
            if codex_node_runtime_error(output):
                node_ok, node_detail = node_version_check(env=provider_process_env(config))
                if not node_ok:
                    return False, node_detail
                return False, "Codex CLI failed to start; reinstall Codex CLI or verify Node.js 20+"
            return False, detail
    finally:
        _CODEX_EXEC_LOCK.release()
