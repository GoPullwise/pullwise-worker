from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

GRAPH_VERIFIED_FINAL_MARKDOWN_MAX_BYTES = 200_000
GRAPH_VERIFIED_DEBUG_MARKDOWN_MAX_BYTES = 50_000
GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES = 512_000


def graph_verified_codex_env(config: WorkerConfig) -> dict[str, str]:
    provider_env = provider_process_env(config)
    return {
        key: provider_env[key]
        for key in (
            "HOME",
            "USERPROFILE",
            "CODEX_HOME",
            "CODEX_SQLITE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "PATH",
        )
        if provider_env.get(key)
    }


def graph_verified_toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def run_graph_verified_review_payload(config: WorkerConfig, job: dict, checkout_dir: Path, progress_callback=None) -> dict:
    from codereview.main import run_review

    agent_config = job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}
    graph_config = agent_config.get("graphVerified") if isinstance(agent_config.get("graphVerified"), dict) else {}
    mode = graph_verified_mode(graph_config.get("mode") or "standard")
    scan_mode = graph_verified_scan_mode(graph_config.get("scanMode") or "full-cached")
    try:
        write_graph_verified_codereview_config(config, checkout_dir, graph_config, mode)
        if progress_callback is None:
            final_path = run_review(checkout_dir, mode=mode, scan_mode=scan_mode)
        else:
            final_path = run_review(checkout_dir, mode=mode, scan_mode=scan_mode, progress=progress_callback)
    except ProcessCancelled as exc:
        raise WorkerJobCancelled(str(exc)) from exc
    except Exception as exc:
        return graph_verified_failed_report(mode, scan_mode, redact_secrets(str(exc), config))
    reports = final_path.parent
    report_error = graph_verified_report_artifact_error(final_path, checkout_dir)
    if report_error:
        return graph_verified_failed_report(mode, scan_mode, report_error)
    confirmed = graph_verified_read_json_artifact(reports / "confirmed.json", [])
    rejected = graph_verified_read_json_artifact(reports / "rejected.json", [])
    final_json = graph_verified_read_json_artifact(reports / "final.json", {"confirmed": []})
    pipeline_summary = graph_verified_read_json_artifact(reports / "summary.json", {})
    report_counts = (
        pipeline_summary.get("reports")
        if isinstance(pipeline_summary, dict) and isinstance(pipeline_summary.get("reports"), dict)
        else {}
    )
    run_id = graph_verified_run_id(final_path.parent.parent.name)
    return {
        "version": "graph-verified-code-review/1",
        "runId": run_id,
        "mode": mode,
        "scanMode": scan_mode,
        "scope": "full-repository",
        "confirmedCount": len(confirmed) if isinstance(confirmed, list) else 0,
        "rejectedCount": len(rejected) if isinstance(rejected, list) else 0,
        "blockedCount": graph_verified_count(report_counts.get("blocked")),
        "finalMarkdown": graph_verified_read_text_artifact(final_path, GRAPH_VERIFIED_FINAL_MARKDOWN_MAX_BYTES),
        "debugMarkdown": graph_verified_read_text_artifact(reports / "debug.md", GRAPH_VERIFIED_DEBUG_MARKDOWN_MAX_BYTES),
        "finalJson": final_json if isinstance(final_json, dict) else {"confirmed": []},
        "summary": pipeline_summary if isinstance(pipeline_summary, dict) else {},
    }


def graph_verified_failed_report(mode: str, scan_mode: str, error: object) -> dict:
    return {
        "version": "graph-verified-code-review/1",
        "mode": mode,
        "scanMode": scan_mode,
        "scope": "full-repository",
        "confirmedCount": 0,
        "rejectedCount": 0,
        "blockedCount": 1,
        "debugMarkdown": f"Graph-verified review failed before confirmation: {clean_protocol_text(error, 1000)}",
        "finalJson": {"confirmed": []},
    }


def graph_verified_run_id(value: object) -> str:
    return clean_protocol_text(value, 128) or "run"


def graph_verified_report_artifact_error(final_path: Path, checkout_dir: Path | None = None) -> str:
    if checkout_dir is not None:
        location_error = graph_verified_report_location_error(final_path, checkout_dir)
        if location_error:
            return location_error
    if not graph_verified_regular_file(final_path):
        return "GraphVerified final markdown report is missing."
    reports = final_path.parent
    debug_path = reports / "debug.md"
    if debug_path.exists() and not graph_verified_regular_file(debug_path):
        return "GraphVerified report artifact must not be a symlink: debug.md."
    checks = (
        ("confirmed.json", list),
        ("rejected.json", list),
        ("final.json", dict),
        ("summary.json", dict),
    )
    for filename, expected_type in checks:
        path = reports / filename
        if path.is_symlink():
            return f"GraphVerified report artifact must not be a symlink: {filename}."
        if not path.is_file():
            return f"GraphVerified report artifact is missing: {filename}."
        try:
            payload = json.loads(read_no_follow_text_file(path, max_bytes=GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"GraphVerified report artifact is unreadable: {filename}: {exc}."
        if not isinstance(payload, expected_type):
            return f"GraphVerified report artifact has invalid shape: {filename}."
    return ""


def graph_verified_report_location_error(final_path: Path, checkout_dir: Path) -> str:
    if final_path.name != "final.md" or final_path.parent.name != "reports":
        return "GraphVerified final markdown report path is invalid."
    try:
        resolved_final = final_path.resolve(strict=True)
        expected_runs = (checkout_dir / ".codereview" / "runs").resolve(strict=False)
        resolved_final.relative_to(expected_runs)
    except (OSError, RuntimeError, ValueError):
        return "GraphVerified final markdown report is outside the checkout run directory."
    if resolved_final.parent.name != "reports" or resolved_final.name != "final.md":
        return "GraphVerified final markdown report path is invalid."
    return ""


def graph_verified_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def graph_verified_read_text_artifact(path: Path, max_bytes: int) -> str:
    if not graph_verified_regular_file(path):
        return ""
    try:
        limit = max(0, int(max_bytes or 0))
    except (TypeError, ValueError, OverflowError):
        limit = 0
    if limit <= 0:
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read(limit + 1)[:limit].decode("utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""
    finally:
        if fd >= 0:
            os.close(fd)


def graph_verified_read_json_artifact(path: Path, default: object) -> object:
    if not graph_verified_regular_file(path):
        return default
    try:
        return json.loads(read_no_follow_text_file(path, max_bytes=GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def graph_verified_progress_message(value: object) -> str:
    source = value if isinstance(value, dict) else {}
    message = clean_protocol_text(source.get("message"), 300)
    if message:
        return message
    stage = clean_protocol_text(source.get("stage"), 80)
    current = source.get("current")
    total = source.get("total")
    if stage and current is not None and total is not None:
        return f"GraphVerified: {stage} {graph_verified_count(current)}/{graph_verified_count(total)}"
    if stage:
        return f"GraphVerified: {stage}"
    return "Running GraphVerified review"


def graph_verified_progress_logs_summary(value: object) -> str:
    source = value if isinstance(value, dict) else {}
    stage = clean_protocol_text(source.get("stage"), 80)
    task_id = clean_protocol_text(source.get("taskId") or source.get("task_id"), 160)
    run_id = clean_protocol_text(source.get("runId") or source.get("run_id"), 80)
    current = source.get("current")
    total = source.get("total")
    parts = []
    if run_id:
        parts.append(f"run={run_id}")
    if stage:
        parts.append(f"stage={stage}")
    if current is not None and total is not None:
        parts.append(f"progress={graph_verified_count(current)}/{graph_verified_count(total)}")
    if task_id:
        parts.append(f"task={task_id}")
    return " ".join(parts)


def write_graph_verified_codereview_config(config: WorkerConfig, checkout_dir: Path, graph_config: dict, mode: str) -> None:
    root = checkout_dir / ".codereview"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    current: dict = {}
    current["mode"] = graph_verified_mode(mode)
    scan_mode = graph_verified_scan_mode(graph_config.get("scanMode") or "full-cached")
    current["scan"] = {
        "mode": scan_mode,
        "include_untracked": True,
        "fail_on_source_change": True,
    }
    current["scope"] = {
        "exclude": [".git/**", ".codereview/**", "node_modules/**", "vendor/**", "dist/**", "build/**", "coverage/**", "target/**"],
        "max_text_file_bytes": 1000000,
        "inventory_excluded_files": True,
    }
    current["graph"] = {
        "schema_version": "3",
        "prompt_version": "graph-v3",
        "full_inventory": True,
        "incremental": scan_mode != "full-strict",
        "target_shards": 12,
        "max_shard_files": 25,
        "max_shard_bytes": 500000,
        "large_file_bytes": 120000,
        "double_map_high_risk": True,
        "max_repair_rounds": 2,
        "use_sqlite_index": True,
        "codex_tool_extractor": graph_config.get("codexToolExtractor") is not False,
        "tool_extractor_max_rounds": graph_verified_positive_int(
            graph_config.get("toolExtractorMaxRounds"),
            default=3,
            minimum=1,
            maximum=8,
        ),
        "tool_extractor_timeout_seconds": graph_verified_positive_int(
            graph_config.get("toolExtractorTimeoutSeconds"),
            default=180,
            minimum=30,
            maximum=1800,
        ),
        "codex_census": graph_config.get("codexCensus") is True,
        "codex_mappers": graph_config.get("codexMappers") is True,
        "codex_linker": graph_config.get("codexLinker") is True,
        "codex_graph_audit": graph_config.get("codexGraphAudit") is True,
        "mapper_subagent_limit": 6,
        "map_parallel": 2,
        "graph_timeout_seconds": 960,
    }
    current["review"] = {
        "require_baseline_for_every_unit": True,
        "require_boundary_review": True,
        "require_global_review": True,
        "max_context_repair_rounds": 1,
        "max_candidates_per_finder": 3,
        "default_upstream_depth": 1,
        "default_downstream_depth": 1,
        "high_risk_upstream_depth": 2,
        "high_risk_downstream_depth": 2,
        "max_unit_nodes": 500,
        "max_unit_paths": 30,
        "max_context_chars": 80000,
    }
    current["context"] = {
        "enabled": graph_config.get("contextEnabled") is not False,
        "timeout_seconds": graph_verified_positive_int(
            graph_config.get("contextTimeoutSeconds"),
            default=300,
            minimum=60,
            maximum=1800,
        ),
    }
    current["codex"] = {
        "command": getattr(config, "codex_command", "") or "codex",
        "model": getattr(config, "codex_model", "") or "",
        "reasoning_effort": getattr(config, "codex_reasoning_effort", "") or "high",
        "env": graph_verified_codex_env(config),
    }
    current["finders"] = {
        "enabled": True,
        "max_workers": graph_verified_positive_int(graph_config.get("finderMaxParallel"), default=6, minimum=1, maximum=6),
        "turn_parallel": graph_verified_positive_int(graph_config.get("finderTurnParallel"), default=1, minimum=1, maximum=6),
        "max_turns_per_scan": graph_verified_positive_int(graph_config.get("finderMaxTurnsPerScan"), default=3, minimum=1, maximum=12),
        "max_jobs_per_subagent": graph_verified_positive_int(graph_config.get("finderMaxJobsPerSubagent"), default=18, minimum=1, maximum=200),
        "timeout_seconds": graph_verified_positive_int(graph_config.get("finderTimeoutSeconds"), default=600, minimum=60, maximum=3600),
    }
    repro_limit = graph_verified_repro_limit(graph_config.get("maxRepro"), mode)
    current["repro"] = {
        "enabled": True,
        "max_workers": graph_verified_positive_int(graph_config.get("reproMaxParallel"), default=2, minimum=1, maximum=8),
        "timeout_seconds": graph_verified_positive_int(graph_config.get("reproTimeoutSeconds"), default=900, minimum=60, maximum=7200),
        "max_repro": repro_limit,
        "require_red_green": graph_config.get("requireRedGreen") is True,
    }
    current["candidates"] = {
        "max_per_finder_per_unit": 3,
        "max_total_for_verification": 60,
        "max_total_for_reproduction": repro_limit,
        "require_expected_behavior_source": True,
    }
    current["scoring"] = {
        "min_score_for_repro": graph_verified_positive_int(graph_config.get("minScoreForRepro"), default=8, minimum=0, maximum=50),
        "always_repro_severities": ["critical", "high"],
    }
    write_no_follow_text_file(path, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def graph_verified_mode(value: object) -> str:
    text = graph_verified_text(value).lower()
    return text if text in {"fast", "standard", "deep"} else "standard"


def graph_verified_scan_mode(value: object) -> str:
    text = graph_verified_text(value).lower()
    return text if text in {"full-cached", "full-strict"} else "full-cached"


def graph_verified_repro_limit(value: object, mode: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return min(100, number)
    return {"fast": 8, "standard": 20, "deep": 50}.get(graph_verified_mode(mode), 20)


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
