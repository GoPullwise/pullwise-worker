from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

GRAPH_VERIFIED_FINAL_MARKDOWN_MAX_BYTES = 200_000
GRAPH_VERIFIED_DEBUG_MARKDOWN_MAX_BYTES = 50_000
GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES = 512_000
GRAPH_VERIFIED_INTERNAL_DIAGNOSTIC_MAX_ITEMS = 50
GRAPH_VERIFIED_PROGRESS_START = PHASE_PROGRESS["index"] + 1
GRAPH_VERIFIED_PROGRESS_COMPLETE = PHASE_PROGRESS["report"]
GRAPH_VERIFIED_PROGRESS_RANGES = {
    "setup": (GRAPH_VERIFIED_PROGRESS_START, 28),
    "inventory": (28, 30),
    "snapshot": (30, 32),
    "census": (32, 34),
    "graph": (34, 50),
    "repository": (50, 53),
    "review_units": (53, 55),
    "finder": (55, 80),
    "candidates": (80, 82),
    "verification": (82, 94),
    "reproduction": (89, 92),
    "judge": (92, 94),
    "report": (94, GRAPH_VERIFIED_PROGRESS_COMPLETE),
}


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
            "PULLWISE_CODEX_MAX_INPUT_CHARS",
            "PULLWISE_CODEX_INPUT_MAX_CHARS",
            "PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS",
            "PULLWISE_CODEX_APP_SERVER_MAX_TURNS",
            "PULLWISE_CODEX_APP_SERVER_LOCK_TIMEOUT_SECONDS",
        )
        if provider_env.get(key)
    }


def graph_verified_toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def run_graph_verified_review_payload(config: WorkerConfig, job: dict, checkout_dir: Path, progress_callback=None) -> dict:
    from codereview.app_server_runner import close_app_server_clients
    from codereview.simple_review import run_review

    agent_config = job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}
    graph_config = agent_config.get("graphVerified") if isinstance(agent_config.get("graphVerified"), dict) else {}
    mode = graph_verified_mode(graph_config.get("mode") or "standard")
    scan_mode = graph_verified_scan_mode(graph_config.get("scanMode") or "full-cached")
    try:
        write_graph_verified_codereview_config(config, checkout_dir, graph_config, mode, job=job)
        if progress_callback is None:
            final_path = run_review(checkout_dir, mode=mode, scan_mode=scan_mode)
        else:
            final_path = run_review(checkout_dir, mode=mode, scan_mode=scan_mode, progress=progress_callback)
    except ProcessCancelled as exc:
        raise WorkerJobCancelled(str(exc)) from exc
    except Exception as exc:
        detail = redact_secrets(str(exc), config)
        if codex_readiness_failure_cacheable(detail):
            mark_codex_auth_failure(config, detail)
        return graph_verified_failed_report(mode, scan_mode, detail)
    finally:
        close_app_server_clients("GraphVerified review complete")
    reports = final_path.parent
    report_error = graph_verified_report_artifact_error(final_path, checkout_dir)
    if report_error:
        return graph_verified_failed_report(mode, scan_mode, report_error)
    confirmed = graph_verified_read_json_artifact(reports / "confirmed.json", [])
    rejected = graph_verified_read_json_artifact(reports / "rejected.json", [])
    final_json = graph_verified_read_json_artifact(reports / "final.json", {"confirmed": []})
    pipeline_summary = graph_verified_read_json_artifact(reports / "summary.json", {})
    internal_diagnostics = graph_verified_internal_diagnostics(
        graph_verified_read_json_artifact(reports / "diagnostics.json", {})
    )
    report_counts = (
        pipeline_summary.get("reports")
        if isinstance(pipeline_summary, dict) and isinstance(pipeline_summary.get("reports"), dict)
        else {}
    )
    run_id = graph_verified_run_id(final_path.parent.parent.name)
    payload = {
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
    if internal_diagnostics:
        payload["internalDiagnostics"] = internal_diagnostics
    return payload


def graph_verified_internal_diagnostics(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    if not source:
        return {}
    reason_counts = graph_verified_internal_reason_counts(source.get("reasonCounts"))
    internal_rejections = graph_verified_internal_rejections(source.get("internalRejections"))
    selected_candidates = graph_verified_internal_selected_candidates(source.get("selectedCandidates"))
    payload = {
        "schemaVersion": graph_verified_count(source.get("schemaVersion")) or 1,
        "selectedCandidateCount": graph_verified_count(source.get("selectedCandidateCount")),
        "internalRejectionCount": graph_verified_count(source.get("internalRejectionCount")),
    }
    if reason_counts:
        payload["reasonCounts"] = reason_counts
    if internal_rejections:
        payload["internalRejections"] = internal_rejections
    if selected_candidates:
        payload["selectedCandidates"] = selected_candidates
    return payload if any(key in payload for key in ("reasonCounts", "internalRejections", "selectedCandidates")) else {}


def graph_verified_internal_reason_counts(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for raw_item in raw_items[:GRAPH_VERIFIED_INTERNAL_DIAGNOSTIC_MAX_ITEMS]:
        if not isinstance(raw_item, dict):
            continue
        reason = clean_protocol_text(raw_item.get("reason"), 800)
        count = graph_verified_count(raw_item.get("count"))
        if not reason or not count:
            continue
        item = {"reason": reason, "count": count}
        stage = clean_protocol_text(raw_item.get("stage"), 80)
        if stage:
            item["stage"] = stage
        items.append(item)
    return items


def graph_verified_internal_rejections(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for raw_item in raw_items[:GRAPH_VERIFIED_INTERNAL_DIAGNOSTIC_MAX_ITEMS]:
        if not isinstance(raw_item, dict):
            continue
        reason = clean_protocol_text(raw_item.get("reason"), 800)
        candidate_id = clean_protocol_text(raw_item.get("candidate_id"), 160)
        if not reason and not candidate_id:
            continue
        item = {"reason": reason}
        stage = clean_protocol_text(raw_item.get("stage"), 80)
        if stage:
            item["stage"] = stage
        if candidate_id:
            item["candidateId"] = candidate_id
        items.append(item)
    return items


def graph_verified_internal_selected_candidates(value: object) -> list[dict]:
    raw_items = value if isinstance(value, list) else []
    items = []
    for raw_item in raw_items[:GRAPH_VERIFIED_INTERNAL_DIAGNOSTIC_MAX_ITEMS]:
        if not isinstance(raw_item, dict):
            continue
        candidate_id = clean_protocol_text(raw_item.get("candidate_id"), 160)
        if not candidate_id:
            continue
        item = {"candidateId": candidate_id}
        for source_key, target_key, limit in (
            ("unit_id", "unitId", 160),
            ("severity", "severity", 20),
            ("category", "category", 80),
            ("title", "title", 240),
        ):
            text = clean_protocol_text(raw_item.get(source_key), limit)
            if text:
                item[target_key] = text
        evidence = raw_item.get("primaryEvidence") if isinstance(raw_item.get("primaryEvidence"), dict) else {}
        evidence_payload = {}
        for source_key, target_key, limit in (("file", "file", 300), ("line", "line", 40), ("symbol", "symbol", 160)):
            text = clean_protocol_text(evidence.get(source_key), limit)
            if text:
                evidence_payload[target_key] = text
        if evidence_payload:
            item["primaryEvidence"] = evidence_payload
        items.append(item)
    return items


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
    diagnostics_path = reports / "diagnostics.json"
    if diagnostics_path.exists():
        if diagnostics_path.is_symlink():
            return "GraphVerified report artifact must not be a symlink: diagnostics.json."
        try:
            payload = json.loads(read_no_follow_text_file(diagnostics_path, max_bytes=GRAPH_VERIFIED_JSON_ARTIFACT_MAX_BYTES))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"GraphVerified report artifact is unreadable: diagnostics.json: {exc}."
        if not isinstance(payload, dict):
            return "GraphVerified report artifact has invalid shape: diagnostics.json."
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
    if not graph_verified_regular_file(path) or path.is_symlink():
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
        if path.is_symlink():
            return ""
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
    task_status = clean_protocol_text(source.get("taskStatus") or source.get("task_status"), 40)
    blocked_reason = clean_protocol_text(source.get("blockedReason") or source.get("blocked_reason"), 320)
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
    if task_status:
        parts.append(f"status={task_status}")
    if "candidateCount" in source or "candidate_count" in source:
        parts.append(f"candidates={graph_verified_count(source.get('candidateCount', source.get('candidate_count')))}")
    if "contextRequestCount" in source or "context_request_count" in source:
        parts.append(f"context_requests={graph_verified_count(source.get('contextRequestCount', source.get('context_request_count')))}")
    if "exitCode" in source or "exit_code" in source:
        parts.append(f"exit_code={graph_verified_count(source.get('exitCode', source.get('exit_code')))}")
    if source.get("timedOut") is True or source.get("timed_out") is True:
        parts.append("timed_out=true")
    if "promptChars" in source or "prompt_chars" in source:
        parts.append(f"prompt_chars={graph_verified_count(source.get('promptChars', source.get('prompt_chars')))}")
    if "inputLimitChars" in source or "input_limit_chars" in source:
        parts.append(f"input_limit_chars={graph_verified_count(source.get('inputLimitChars', source.get('input_limit_chars')))}")
    input_limit_source = clean_protocol_text(source.get("inputLimitSource") or source.get("input_limit_source"), 80)
    if input_limit_source:
        parts.append(f"input_limit_source={input_limit_source}")
    if "batchTaskCount" in source or "batch_task_count" in source:
        parts.append(f"batch_tasks={graph_verified_count(source.get('batchTaskCount', source.get('batch_task_count')))}")
    if "missingBaselineReviewUnitCount" in source or "missing_baseline_review_unit_count" in source:
        parts.append(
            f"missing_baseline={graph_verified_count(source.get('missingBaselineReviewUnitCount', source.get('missing_baseline_review_unit_count')))}"
        )
    missing_units = graph_verified_clean_list(source.get("missingBaselineReviewUnitIds") or source.get("missing_baseline_review_unit_ids"), 8, 80)
    if missing_units:
        parts.append(f"missing_units={','.join(missing_units)}")
    if blocked_reason:
        parts.append(f"reason={blocked_reason}")
    return " ".join(parts)


def graph_verified_clean_list(value: object, limit: int, item_max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        text = clean_protocol_text(item, item_max_chars)
        if text:
            output.append(text)
        if len(output) >= limit:
            break
    return output

def graph_verified_progress_percent(value: object) -> int:
    source = value if isinstance(value, dict) else {}
    stage = clean_protocol_text(source.get("stage"), 80)
    start, end = GRAPH_VERIFIED_PROGRESS_RANGES.get(
        stage,
        (GRAPH_VERIFIED_PROGRESS_START, GRAPH_VERIFIED_PROGRESS_START),
    )
    current = source.get("current")
    total = source.get("total")
    try:
        current_count = max(0, int(current))
        total_count = max(0, int(total))
    except (TypeError, ValueError):
        return max(0, min(100, int(start)))
    if total_count <= 0:
        return max(0, min(100, int(start)))
    fraction = max(0.0, min(1.0, current_count / total_count))
    progress = int(round(start + ((end - start) * fraction)))
    return max(0, min(100, progress))


def write_graph_verified_codereview_config(
    config: WorkerConfig,
    checkout_dir: Path,
    graph_config: dict,
    mode: str,
    *,
    job: dict | None = None,
) -> None:
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
    current["codex"] = {
        "command": getattr(config, "codex_command", "") or "codex",
        "model": getattr(config, "codex_model", "") or "",
        "reasoning_effort": getattr(config, "codex_reasoning_effort", "") or "high",
        "env": graph_verified_codex_env(config),
        "max_input_chars": graph_verified_nonnegative_int(graph_config.get("codexMaxInputChars"), default=0),
    }
    repro_limit = graph_verified_repro_limit(graph_config.get("maxRepro"), mode)
    job_source = job if isinstance(job, dict) else {}
    output_language = graph_verified_text(
        job_source.get("review_output_language_label")
        or job_source.get("reviewOutputLanguageLabel")
        or job_source.get("review_output_language")
        or job_source.get("reviewOutputLanguage")
    ) or "English"
    discovery_parallel = graph_config.get("simpleDiscoveryParallel")
    if discovery_parallel is None:
        discovery_parallel = graph_config.get("finderTurnParallel")
    verification_parallel = graph_config.get("simpleVerificationParallel")
    if verification_parallel is None:
        verification_parallel = graph_config.get("reproMaxParallel")
    current["simple"] = {
        "engine": "simple-full-repository/1",
        "discovery_turns": graph_verified_positive_int(
            graph_config.get("finderMaxTurnsPerScan"),
            default={"fast": 2, "standard": 3, "deep": 4}.get(graph_verified_mode(mode), 3),
            minimum=1,
            maximum=16,
        ),
        "max_discovery_turns": graph_verified_positive_int(
            graph_config.get("simpleMaxDiscoveryTurns"),
            default=48,
            minimum=1,
            maximum=64,
        ),
        "discovery_parallel": graph_verified_positive_int(
            discovery_parallel,
            default=0,
            minimum=0,
            maximum=4,
        ),
        "verification_parallel": graph_verified_positive_int(
            verification_parallel,
            default=0,
            minimum=0,
            maximum=4,
        ),
        "subagents_per_turn": graph_verified_positive_int(
            graph_config.get("subagentsPerTurn"),
            default=3,
            minimum=1,
            maximum=4,
        ),
        "max_candidates": repro_limit,
        "max_candidates_per_unit": graph_verified_positive_int(
            graph_config.get("maxCandidatesPerUnit"),
            default=2,
            minimum=1,
            maximum=4,
        ),
        "max_unit_files": 40,
        "max_unit_bytes": 500000,
        "max_batch_files": graph_verified_positive_int(
            graph_config.get("simpleMaxBatchFiles"),
            default=120,
            minimum=10,
            maximum=400,
        ),
        "max_batch_bytes": graph_verified_positive_int(
            graph_config.get("simpleMaxBatchBytes"),
            default=1500000,
            minimum=100000,
            maximum=5000000,
        ),
        "discovery_timeout_seconds": graph_verified_positive_int(
            graph_config.get("finderTimeoutSeconds"), default=900, minimum=60, maximum=3600
        ),
        "verification_timeout_seconds": graph_verified_positive_int(
            graph_config.get("reproTimeoutSeconds"), default=1200, minimum=60, maximum=7200
        ),
        "scan_deadline_seconds": graph_verified_positive_int(
            graph_config.get("simpleScanDeadlineSeconds") or graph_config.get("scanDeadlineSeconds"),
            default={"fast": 1800, "standard": 3600, "deep": 7200}.get(graph_verified_mode(mode), 3600),
            minimum=0,
            maximum=21600,
        ),
        "output_language": output_language,
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
    return {"fast": 6, "standard": 10, "deep": 20}.get(graph_verified_mode(mode), 10)


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


def graph_verified_nonnegative_int(value: object, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def graph_verified_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
