from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

def result_checksum(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def redact_secrets(text: str, config: WorkerConfig | None = None) -> str:
    redacted = str(text or "")
    if config and config.worker_token:
        redacted = redacted.replace(config.worker_token, "[redacted]")
    redacted = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:[redacted]@", redacted)
    return redacted


def write_scan_summary(config: WorkerConfig, job_id: str, status: str, duration_ms: int, error: str) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    path = config.log_dir / "scan-summary.log"
    line = json.dumps(
        {
            "time": int(time.time()),
            "job_id": job_id,
            "status": status,
            "duration_ms": duration_ms,
            "error": redact_secrets(error, config),
        },
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")
    trim_file_to_last_bytes(path, config.scan_summary_log_max_bytes)


def provider_command_scope_check(command: str, config: WorkerConfig, label: str) -> tuple[bool, str]:
    raw = str(command or "").strip()
    if not raw:
        return False, f"{label} command missing"
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


def worker_readiness_checks(config: WorkerConfig) -> tuple[list[tuple[str, bool, str]], bool]:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("server_url", server_url_allowed(config.server_url, allow_insecure=config.allow_insecure_server_url), config.server_url))
    checks.append(("worker_token", bool(config.worker_token), "configured" if config.worker_token else "missing"))
    agent_configs_ok, agent_configs_detail, agent_configs = worker_agent_configs_check(config)
    checks.append(("agent_configs", agent_configs_ok, agent_configs_detail))
    checks.append(("max_concurrent_jobs", config.max_concurrent_jobs > 0, str(config.max_concurrent_jobs)))

    git_ok, git_detail = command_ok(["git", "--version"])
    checks.append(("git", git_ok, git_detail))
    ready_providers: list[str] = []
    required_providers = subscription_plan_required_providers(agent_configs) if agent_configs_ok else []
    if "codex" in required_providers:
        node_ok, node_detail = node_version_check()
        checks.append(("node", node_ok, node_detail))
        codex_scope_ok, codex_scope_detail = provider_command_scope_check(config.codex_command, config, "Codex")
        codex_cli_ok, codex_cli_detail = (
            command_ok([config.codex_command, "--version"]) if codex_scope_ok else (False, codex_scope_detail)
        )
        checks.append(("codex", codex_cli_ok, codex_cli_detail))
        codex_login_ok, codex_login_detail = codex_ready_check(config) if codex_cli_ok else (False, "skipped until codex CLI passes --version")
        checks.append(("codex_ready", codex_login_ok, codex_login_detail))
        if node_ok and codex_cli_ok and codex_login_ok:
            ready_providers.append("codex")
    if "opencode" in required_providers:
        opencode_scope_ok, opencode_scope_detail = provider_command_scope_check(config.opencode_command, config, "OpenCode")
        opencode_ok, opencode_detail = (
            command_ok([config.opencode_command, "--version"]) if opencode_scope_ok else (False, opencode_scope_detail)
        )
        checks.append(("opencode", opencode_ok, opencode_detail))
        opencode_auth_ok, opencode_auth_detail = (
            opencode_auth_check(config, agent_configs) if opencode_ok and agent_configs_ok else (
                False,
                "skipped until opencode CLI passes --version" if not opencode_ok else agent_configs_detail,
            )
        )
        checks.append(("opencode_ready", opencode_auth_ok, opencode_auth_detail))
        if opencode_ok and opencode_auth_ok:
            ready_providers.append("opencode")
    provider_ready = bool(ready_providers)
    provider_ready_detail = (
        ", ".join(ready_providers)
        if provider_ready
        else "no configured provider is ready" if agent_configs_ok else "subscription plan agent configs unavailable"
    )
    checks.append(("provider_ready", provider_ready, provider_ready_detail))

    for label, path in (("checkout_root", config.work_dir), ("log_dir", config.log_dir)):
        ok, detail = writable_path_check(path)
        checks.append((label, ok, detail))
    checks.append(("disk_space", *disk_space_check(config.work_dir)))
    return checks, provider_ready


def first_failed_check(checks: list[tuple[str, bool, str]]) -> tuple[str, bool, str] | None:
    provider_ready = readiness_check_ok(checks, "provider_ready")
    optional_provider_checks = {"node", "codex", "codex_ready", "opencode", "opencode_ready"}
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
    checks, _provider_ready = worker_readiness_checks(config)
    codex_ready = readiness_check_ok(checks, "codex_ready")
    systemd_ok, systemd_detail = command_ok(["systemctl", "is-active", "pullwise-worker"])
    checks.append(("systemd", systemd_ok, systemd_detail))
    heartbeat_ok = True
    heartbeat_detail = "ok"
    doctor_required_ok = first_failed_check(checks) is None
    try:
        PullwiseClient(config).heartbeat(
            last_error=None,
            doctor_status="ok" if doctor_required_ok else "degraded",
            codex_ready=codex_ready,
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
    opencode_check = next((check for check in checks if check[0] == "opencode_ready"), None)
    if opencode_check and not opencode_check[1]:
        print(f"OpenCode interactive provider selection. Run: {opencode_auth_command(config)}")
    return first_failed_check(checks) is None


def command_ok(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        detail = (completed.stdout or completed.stderr).strip().splitlines()
        return completed.returncode == 0, detail[0] if detail else f"exit {completed.returncode}"
    except FileNotFoundError:
        return False, "not found"
    except Exception as exc:
        return False, str(exc)


def opencode_auth_output_plain(output: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(output or ""))


def opencode_auth_output_has_ready_provider(output: str, provider: str) -> bool:
    output = opencode_auth_output_plain(output)
    provider_id = provider.lower()
    provider_pattern = re.compile(rf"(^|[^a-z0-9_-]){re.escape(provider_id)}([^a-z0-9_-]|$)")
    catalog_markers = (
        "available providers",
        "supported providers",
        "select a provider",
        "choose a provider",
    )
    ready_markers = (
        "authenticated",
        "logged in",
        "logged-in",
        "active",
        "configured",
        "connected",
        "valid",
        "enabled",
        "api key",
        "api-key",
        "apikey",
        "true",
        "yes",
        "✓",
        "✔",
    )
    missing_markers = (
        "not authenticated",
        "not logged in",
        "not logged-in",
        "unauthenticated",
        "missing",
        "no credentials",
        "no api key",
        "no api-key",
        "no apikey",
        "invalid",
        "false",
        "disabled",
    )
    provider_catalog = any(marker in output.lower() for marker in catalog_markers)
    credential_listing = "credential" in output.lower()
    api_credential_pattern = re.compile(r"(^|[^a-z0-9_-])api([^a-z0-9_-]|$)")
    for line in output.splitlines():
        lowered = line.lower()
        if not provider_pattern.search(lowered):
            continue
        if any(marker in lowered for marker in missing_markers):
            continue
        if any(marker in lowered for marker in ready_markers):
            return True
        if credential_listing and api_credential_pattern.search(lowered):
            return True
        if not provider_catalog and lowered.strip(" \t-*") == provider_id:
            return True
    return False


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
        provider_chain = agent_config_provider_chain(agent_config)
        if not provider_chain:
            return f"subscription plan agent configs invalid: {plan}.providerChain is required"
        if "codex" in provider_chain:
            codex_config = agent_config.get("codex") if isinstance(agent_config.get("codex"), dict) else {}
            if not normalized_agent_config_text(codex_config.get("model")):
                return f"subscription plan agent configs invalid: {plan}.codex.model is required"
            if not normalized_agent_reasoning_level(codex_config.get("reasoningEffort")):
                return f"subscription plan agent configs invalid: {plan}.codex.reasoningEffort is required"
        if "opencode" in provider_chain:
            opencode_config = agent_config.get("opencode") if isinstance(agent_config.get("opencode"), dict) else {}
            if not normalized_agent_config_text(opencode_config.get("model")):
                return f"subscription plan agent configs invalid: {plan}.opencode.model is required"
            if not normalized_agent_reasoning_level(opencode_config.get("variant")):
                return f"subscription plan agent configs invalid: {plan}.opencode.variant is required"
    return ""


def agent_config_provider_chain(agent_config: dict) -> list[str]:
    raw_chain = agent_config.get("providerChain") if isinstance(agent_config, dict) else None
    if isinstance(raw_chain, list):
        providers: list[str] = []
        for item in raw_chain:
            provider = str(item or "").strip().lower()
            if provider in SUPPORTED_REVIEW_PROVIDERS and provider not in providers:
                providers.append(provider)
        return providers
    return []


def subscription_plan_required_providers(payload: dict | None) -> list[str]:
    required: list[str] = []
    for agent_config in subscription_plan_agent_configs(payload).values():
        for provider in agent_config_provider_chain(agent_config):
            if provider not in required:
                required.append(provider)
    return required


def provider_id_from_model(model: str | None) -> str:
    raw_model = str(model or "").strip()
    provider = raw_model.split("/", 1)[0].strip().lower() if raw_model else ""
    return provider


def opencode_required_provider_specs(agent_configs: dict | None) -> list[tuple[str, str, str]]:
    requirements: list[tuple[str, str, str]] = []
    for plan, agent_config in subscription_plan_agent_configs(agent_configs).items():
        if "opencode" not in agent_config_provider_chain(agent_config):
            continue
        opencode_config = agent_config.get("opencode") if isinstance(agent_config.get("opencode"), dict) else {}
        model = str(opencode_config.get("model") or "").strip()
        requirements.append((plan, provider_id_from_model(model), model))
    return requirements


def opencode_auth_check(config: WorkerConfig, agent_configs: dict | None = None) -> tuple[bool, str]:
    if agent_configs is None:
        agent_configs_ok, agent_configs_detail, agent_configs = worker_agent_configs_check(config)
        if not agent_configs_ok:
            return False, agent_configs_detail
    requirements = opencode_required_provider_specs(agent_configs)
    if not requirements:
        return True, "no subscription plan requires opencode"
    scope_ok, scope_detail = provider_command_scope_check(config.opencode_command, config, "OpenCode")
    if not scope_ok:
        return False, scope_detail
    try:
        completed = subprocess.run(
            [config.opencode_command, "auth", "list"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except FileNotFoundError:
        return False, "opencode not found"
    except Exception as exc:
        return False, str(exc)

    output = redact_secrets("\n".join([completed.stdout or "", completed.stderr or ""]).strip(), config)
    detail = output.splitlines()[0] if output else f"exit {completed.returncode}"
    if completed.returncode != 0:
        return False, detail
    authenticated_providers = []
    missing_requirements = []
    for plan, provider, _model in requirements:
        if opencode_auth_output_has_ready_provider(output, provider):
            if provider not in authenticated_providers:
                authenticated_providers.append(provider)
            continue
        missing_requirements.append((plan, provider))
    if not missing_requirements:
        return True, f"authenticated for configured OpenCode providers: {', '.join(authenticated_providers)}"
    missing_detail = ", ".join(f"{plan}={provider}" for plan, provider in missing_requirements)
    return False, f"not authenticated for OpenCode providers required by subscription plans: {missing_detail}"


def node_version_check() -> tuple[bool, str]:
    ok, detail = command_ok(["node", "--version"])
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
        return True, "ready check deferred while codex is running"
    try:
        scope_ok, scope_detail = provider_command_scope_check(config.codex_command, config, "Codex")
        if not scope_ok:
            return False, scope_detail
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "codex-ready.json"
            command = [
                config.codex_command,
                "exec",
                _CODEX_SKIP_GIT_REPO_CHECK_ARG,
                "--ignore-user-config",
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
                node_ok, node_detail = node_version_check()
                if not node_ok:
                    return False, node_detail
                return False, "Codex CLI failed to start; reinstall Codex CLI or verify Node.js 20+"
            return False, detail
    finally:
        _CODEX_EXEC_LOCK.release()


