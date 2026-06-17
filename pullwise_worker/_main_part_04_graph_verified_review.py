from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.


def graph_verified_review_enabled(config: WorkerConfig, job: dict) -> bool:
    return True


def graph_verified_codex_env(config: WorkerConfig) -> dict[str, str]:
    provider_env = provider_process_env(config)
    return {
        key: provider_env[key]
        for key in ("HOME", "USERPROFILE", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "PATH")
        if provider_env.get(key)
    }


def ensure_graph_verified_codegraph_codex_mcp(config: WorkerConfig, checkout_dir: Path, graph_config: dict) -> None:
    command = graph_verified_text(graph_config.get("codegraphCommand")) or "codegraph"
    timeout = max(60, int(getattr(config, "codex_doctor_timeout_seconds", 60) or 60))
    install_command = [command, "install", "--target=codex", "--location=global", "--yes"]
    provider_env = provider_process_env(config)
    try:
        completed = subprocess.run(
            install_command,
            cwd=str(checkout_dir),
            env=provider_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"CodeGraph Codex MCP install failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or ""
        detail = compact_codex_failure_output(str(output), stream_name="timeout") or "timeout"
        raise RuntimeError(f"CodeGraph Codex MCP install timed out after {timeout}s: {detail}") from exc
    if completed.returncode != 0:
        detail = compact_codex_failure_output(
            completed.stderr or completed.stdout,
            stream_name="stderr" if completed.stderr else "stdout",
        )
        raise RuntimeError(
            f"CodeGraph Codex MCP install failed with exit code {completed.returncode}: "
            f"{detail or 'no stderr/stdout'}"
        )
    upsert_graph_verified_codex_mcp_config(provider_env, command)


def upsert_graph_verified_codex_mcp_config(provider_env: dict[str, str], command: str) -> None:
    codex_home = provider_env.get("CODEX_HOME") or provider_home_path(provider_env.get("HOME") or DEFAULT_SERVICE_HOME, ".codex")
    path = Path(codex_home) / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    block = (
        "[mcp_servers.codegraph]\n"
        f"command = {graph_verified_toml_string(command)}\n"
        'args = ["serve", "--mcp"]\n'
    )
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.codegraph\]\r?\n.*?(?=^\[|\Z)")
    if pattern.search(current):
        updated = pattern.sub(block, current, count=1)
    else:
        prefix = current.rstrip()
        updated = f"{prefix}\n\n{block}" if prefix else block
    path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def graph_verified_toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def run_graph_verified_review_payload(config: WorkerConfig, job: dict, checkout_dir: Path, head_ref: str) -> dict:
    from codereview.main import run_review
    from codereview.utils.jsonl import read_json

    agent_config = job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}
    graph_config = agent_config.get("graphVerified") if isinstance(agent_config.get("graphVerified"), dict) else {}
    base_ref = graph_verified_text(job.get("base_commit") or job.get("baseCommit"))
    if not base_ref:
        base_ref = f"{head_ref}^"
    mode = graph_verified_mode(graph_config.get("mode") or "standard")
    try:
        ensure_graph_verified_codegraph_codex_mcp(config, checkout_dir, graph_config)
        write_graph_verified_codereview_config(config, checkout_dir, graph_config, mode)
        final_path = run_review(checkout_dir, base_ref=base_ref, head_ref=head_ref, mode=mode)
    except Exception as exc:
        return {
            "version": "graph-verified-code-review/1",
            "mode": mode,
            "base": base_ref,
            "head": head_ref,
            "confirmedCount": 0,
            "rejectedCount": 0,
            "blockedCount": 1,
            "debugMarkdown": f"Graph-verified review failed before confirmation: {redact_secrets(str(exc), config)}",
            "finalJson": {"confirmed": []},
        }
    reports = final_path.parent
    confirmed = read_json(reports / "confirmed.json", [])
    rejected = read_json(reports / "rejected.json", [])
    final_json = read_json(reports / "final.json", {"confirmed": []})
    pipeline_summary = read_json(reports / "summary.json", {})
    report_counts = (
        pipeline_summary.get("reports")
        if isinstance(pipeline_summary, dict) and isinstance(pipeline_summary.get("reports"), dict)
        else {}
    )
    run_id = final_path.parent.parent.name
    return {
        "version": "graph-verified-code-review/1",
        "runId": run_id,
        "mode": mode,
        "base": base_ref,
        "head": head_ref,
        "confirmedCount": len(confirmed) if isinstance(confirmed, list) else 0,
        "rejectedCount": len(rejected) if isinstance(rejected, list) else 0,
        "blockedCount": graph_verified_count(report_counts.get("blocked")),
        "finalMarkdown": final_path.read_text(encoding="utf-8") if final_path.is_file() else "",
        "debugMarkdown": (reports / "debug.md").read_text(encoding="utf-8") if (reports / "debug.md").is_file() else "",
        "finalJson": final_json if isinstance(final_json, dict) else {"confirmed": []},
        "summary": pipeline_summary if isinstance(pipeline_summary, dict) else {},
    }


def write_graph_verified_codereview_config(config: WorkerConfig, checkout_dir: Path, graph_config: dict, mode: str) -> None:
    root = checkout_dir / ".codereview"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    current: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            current = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            current = {}
    current["mode"] = graph_verified_mode(mode)
    current["codegraph"] = {
        **(current.get("codegraph") if isinstance(current.get("codegraph"), dict) else {}),
        "command": graph_verified_text(graph_config.get("codegraphCommand")) or "codegraph",
        "optional_sync": graph_config.get("syncBeforeRun") is not False,
        "reindex": graph_config.get("forceIndexOnFailure") is True,
    }
    current["codex"] = {
        **(current.get("codex") if isinstance(current.get("codex"), dict) else {}),
        "command": getattr(config, "codex_command", "") or "codex",
        "model": getattr(config, "codex_model", "") or "",
        "reasoning_effort": getattr(config, "codex_reasoning_effort", "") or "high",
        "env": graph_verified_codex_env(config),
    }
    current["finders"] = {
        **(current.get("finders") if isinstance(current.get("finders"), dict) else {}),
        "enabled": True,
        "max_workers": graph_verified_positive_int(graph_config.get("finderMaxParallel"), default=4, minimum=1, maximum=12),
        "timeout_seconds": graph_verified_positive_int(graph_config.get("finderTimeoutSeconds"), default=600, minimum=60, maximum=3600),
    }
    current["repro"] = {
        **(current.get("repro") if isinstance(current.get("repro"), dict) else {}),
        "enabled": True,
        "max_workers": graph_verified_positive_int(graph_config.get("reproMaxParallel"), default=2, minimum=1, maximum=8),
        "timeout_seconds": graph_verified_positive_int(graph_config.get("reproTimeoutSeconds"), default=900, minimum=60, maximum=7200),
        "max_repro": graph_verified_positive_int(graph_config.get("maxRepro"), default=0, minimum=0, maximum=100),
        "require_red_green": graph_config.get("requireRedGreen") is True,
    }
    current["scoring"] = {
        **(current.get("scoring") if isinstance(current.get("scoring"), dict) else {}),
        "min_score_for_repro": graph_verified_positive_int(graph_config.get("minScoreForRepro"), default=8, minimum=0, maximum=50),
        "always_repro_severities": ["critical", "high"],
    }
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def graph_verified_mode(value: object) -> str:
    text = graph_verified_text(value).lower()
    return text if text in {"fast", "standard", "deep"} else "standard"


def graph_verified_text(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(char in text for char in "\r\n\x00"):
        return ""
    return text


def graph_verified_positive_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def graph_verified_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
