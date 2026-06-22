from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.


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
    from codereview.utils.jsonl import read_json

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
        return {
            "version": "graph-verified-code-review/1",
            "mode": mode,
            "scanMode": scan_mode,
            "scope": "full-repository",
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
        "scanMode": scan_mode,
        "scope": "full-repository",
        "confirmedCount": len(confirmed) if isinstance(confirmed, list) else 0,
        "rejectedCount": len(rejected) if isinstance(rejected, list) else 0,
        "blockedCount": graph_verified_count(report_counts.get("blocked")),
        "finalMarkdown": final_path.read_text(encoding="utf-8") if final_path.is_file() else "",
        "debugMarkdown": (reports / "debug.md").read_text(encoding="utf-8") if (reports / "debug.md").is_file() else "",
        "finalJson": final_json if isinstance(final_json, dict) else {"confirmed": []},
        "summary": pipeline_summary if isinstance(pipeline_summary, dict) else {},
    }


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
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            current = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            current = {}
    current["mode"] = graph_verified_mode(mode)
    for stale_key in ("codegraph", "impact"):
        current.pop(stale_key, None)
    scan_mode = graph_verified_scan_mode(graph_config.get("scanMode") or "full-cached")
    current["scan"] = {
        **(current.get("scan") if isinstance(current.get("scan"), dict) else {}),
        "mode": scan_mode,
        "include_untracked": True,
        "fail_on_source_change": True,
    }
    current["scope"] = {
        **(current.get("scope") if isinstance(current.get("scope"), dict) else {}),
        "exclude": [".git/**", ".codereview/**", "node_modules/**", "vendor/**", "dist/**", "build/**", "coverage/**", "target/**"],
        "max_text_file_bytes": 1000000,
        "inventory_excluded_files": True,
    }
    current["graph"] = {
        "schema_version": "3",
        "prompt_version": "graph-v3",
        "full_inventory": True,
        "incremental": scan_mode != "full-strict",
        "target_shards": 6,
        "max_shard_files": 25,
        "max_shard_bytes": 500000,
        "large_file_bytes": 120000,
        "double_map_high_risk": True,
        "max_repair_rounds": 2,
        "use_sqlite_index": True,
        "codex_census": True,
        "codex_mappers": True,
        "mapper_subagent_limit": 6,
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
        "max_unit_nodes": 100,
        "max_unit_paths": 30,
        "max_context_chars": 80000,
    }
    current["context"] = {
        **(current.get("context") if isinstance(current.get("context"), dict) else {}),
        "enabled": graph_config.get("contextEnabled") is not False,
        "timeout_seconds": graph_verified_positive_int(
            graph_config.get("contextTimeoutSeconds"),
            default=300,
            minimum=60,
            maximum=1800,
        ),
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
        "max_workers": graph_verified_positive_int(graph_config.get("finderMaxParallel"), default=6, minimum=1, maximum=6),
        "turn_parallel": graph_verified_positive_int(graph_config.get("finderTurnParallel"), default=2, minimum=1, maximum=6),
        "timeout_seconds": graph_verified_positive_int(graph_config.get("finderTimeoutSeconds"), default=600, minimum=60, maximum=3600),
    }
    repro_limit = graph_verified_repro_limit(graph_config.get("maxRepro"), mode)
    current["repro"] = {
        **(current.get("repro") if isinstance(current.get("repro"), dict) else {}),
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
        **(current.get("scoring") if isinstance(current.get("scoring"), dict) else {}),
        "min_score_for_repro": graph_verified_positive_int(graph_config.get("minScoreForRepro"), default=8, minimum=0, maximum=50),
        "always_repro_severities": ["critical", "high"],
    }
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
