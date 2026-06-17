from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

AGENT_REASONING_LEVELS = {"low", "medium", "high", "xhigh"}
AGENT_CONFIG_TEXT_MAX_LENGTH = 128


def normalized_agent_reasoning_level(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return normalized if normalized in AGENT_REASONING_LEVELS else ""


def normalized_agent_config_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or len(normalized) > AGENT_CONFIG_TEXT_MAX_LENGTH:
        return ""
    if any(char in normalized for char in "\r\n\x00"):
        return ""
    if any(char.isspace() for char in normalized):
        return ""
    return normalized


def normalized_agent_provider(value: object) -> str:
    provider = normalized_agent_config_text(value).lower()
    return provider if provider in SUPPORTED_REVIEW_PROVIDERS else ""


def normalized_agent_provider_chain(value: object) -> list[str]:
    candidates = value if isinstance(value, list) else [value]
    providers: list[str] = []
    for candidate in candidates:
        provider = normalized_agent_provider(candidate)
        if provider and provider not in providers:
            providers.append(provider)
    return providers


def normalized_positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (OverflowError, TypeError, ValueError):
        return 0


def effective_agent_config_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > AGENT_CONFIG_TEXT_MAX_LENGTH:
        return ""
    if any(char in text for char in "\r\n\x00"):
        return ""
    return text


def effective_agent_config_payload(config: WorkerConfig, provider: object = None) -> dict:
    selected_provider = (
        normalized_agent_provider(provider)
        or normalized_agent_provider(getattr(config, "provider", ""))
        or "codex"
    )
    selected_provider = "codex"
    cli = effective_agent_config_value(getattr(config, "codex_command", ""))
    model = effective_agent_config_value(getattr(config, "codex_model", ""))
    reasoning_effort = normalized_agent_reasoning_level(getattr(config, "codex_reasoning_effort", ""))
    return {
        "provider": selected_provider,
        "agent": {
            "cli": selected_provider,
            "command": cli,
            "model": model,
            "reasoningEffort": reasoning_effort,
        },
        "cli": cli,
        "model": model,
        "reasoningEffort": reasoning_effort,
        "codex": {
            "cli": effective_agent_config_value(getattr(config, "codex_command", "")),
            "command": effective_agent_config_value(getattr(config, "codex_command", "")),
            "model": effective_agent_config_value(getattr(config, "codex_model", "")),
            "reasoningEffort": normalized_agent_reasoning_level(getattr(config, "codex_reasoning_effort", "")),
        },
    }


def graph_verified_summary_findings(report: dict) -> list[dict]:
    final_json = report.get("finalJson") if isinstance(report.get("finalJson"), dict) else {}
    confirmed = final_json.get("confirmed") if isinstance(final_json.get("confirmed"), list) else []
    findings = []
    for index, item in enumerate(confirmed):
        if not isinstance(item, dict):
            continue
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
        judge = item.get("judge") if isinstance(item.get("judge"), dict) else {}
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        status = str(judge.get("status") or verification.get("status") or verification.get("verdict") or "").strip().lower()
        if status and status != "confirmed":
            continue
        if judge.get("safe_to_show_user") is False or verification.get("safe_to_show_user") is False:
            continue
        graph_evidence = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
        if not graph_evidence:
            continue
        findings.append(
            {
                "id": clean_protocol_text(candidate.get("candidate_id") or candidate.get("issue_id")) or f"gv-{index + 1}",
                "severity": graph_verified_severity(candidate.get("severity")),
            }
        )
    return findings


def graph_verified_severity(value: object) -> str:
    text = clean_protocol_text(value).lower()
    return text if text in {"critical", "high", "medium", "low", "info"} else "info"


def graph_verified_report_count(report: dict, key: str) -> int:
    try:
        return max(0, int(report.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def graph_verified_completion_payload(report: dict, *, result_status: str) -> dict:
    if not report:
        return {
            "status": "failed",
            "summary": "GraphVerified review did not produce a report.",
            "blockers": ["missing_graph_verified_report"],
        }
    status = "ok" if result_status == "done" else "failed"
    return {
        "status": status,
        "summary": "GraphVerified report produced.",
        "confirmedCount": graph_verified_report_count(report, "confirmedCount"),
        "rejectedCount": graph_verified_report_count(report, "rejectedCount"),
        "blockedCount": graph_verified_report_count(report, "blockedCount"),
        "runId": clean_protocol_text(report.get("runId")),
    }


def worker_config_for_job(base_config: WorkerConfig, job: dict) -> WorkerConfig:
    agent_config = job.get("agentConfig")
    repository_limits = job.get("repositoryLimits")
    if not isinstance(agent_config, dict):
        raise RuntimeError("Worker job is missing server agentConfig.")
    if not isinstance(repository_limits, dict):
        raise RuntimeError("Worker job is missing server repositoryLimits.")
    codex = agent_config.get("codex") if isinstance(agent_config.get("codex"), dict) else {}
    max_repo_files = normalized_positive_int(repository_limits.get("maxFiles"))
    max_repo_bytes = normalized_positive_int(repository_limits.get("maxBytes"))
    provider = normalized_agent_provider(agent_config.get("provider"))
    codex_model = normalized_agent_config_text(codex.get("model"))
    codex_reasoning_effort = normalized_agent_reasoning_level(codex.get("reasoningEffort"))
    if not provider:
        raise RuntimeError("Worker job agentConfig.provider is required.")
    if provider == "codex" and not (codex_model and codex_reasoning_effort):
        raise RuntimeError("Worker job agentConfig.codex model and reasoningEffort are required.")
    if not max_repo_files or not max_repo_bytes:
        raise RuntimeError("Worker job repositoryLimits.maxFiles and maxBytes are required.")
    config = copy.copy(base_config)
    config.provider_chain = [provider]
    config.provider = provider
    if codex_model:
        config.codex_model = codex_model
    if codex_reasoning_effort:
        config.codex_reasoning_effort = codex_reasoning_effort
    config.max_repo_files = max_repo_files
    config.max_repo_bytes = max_repo_bytes
    return config


class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.client = PullwiseClient(config)
        self.last_error: str | None = None
        self._readiness_checked_at = 0.0
        self._doctor_status = "not_ready"
        self._codex_ready = False
        self._ready_providers: list[str] = []
        self._empty_poll_count = 0
        self._error_poll_count = 0
        self._last_cleanup_at = 0.0
        self._machine_metrics_checked_at = 0.0

    def effective_max_concurrent_jobs(self) -> int:
        configured = max(1, int(self.config.max_concurrent_jobs or 1))
        provider_chain = self.config.provider_chain or [self.config.provider]
        providers = {str(provider or "").strip().lower() for provider in provider_chain}
        if "codex" in providers:
            return 1
        return configured

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_resources_if_due([], force=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_concurrent_jobs) as executor:
            running: dict[concurrent.futures.Future[None], dict] = {}

            def collect_finished_jobs(futures: list[concurrent.futures.Future[None]]) -> None:
                for future in futures:
                    job = running.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        self.last_error = f"job {job.get('job_id')} failed unexpectedly: {exc}"[:500]

            while True:
                done = [future for future in running if future.done()]
                collect_finished_jobs(done)
                self.cleanup_resources_if_due(running.values())
                max_running_jobs = self.effective_max_concurrent_jobs()
                free_slots = max(0, max_running_jobs - len(running))
                ready = self.refresh_readiness_if_due()
                loop_error = False
                claimed_jobs = 0
                heartbeat_payload: dict = {}
                machine_metrics = self.machine_metrics_if_due()
                active_job_ids = []
                for job in running.values():
                    try:
                        active_job_ids.append(safe_job_id(job.get("job_id") if isinstance(job, dict) else job))
                    except ValueError:
                        continue
                try:
                    heartbeat_response = self.client.heartbeat(
                        running_jobs=len(running),
                        max_concurrent_jobs=max_running_jobs,
                        active_job_ids=active_job_ids,
                        last_error=self.last_error,
                        doctor_status=self._doctor_status,
                        codex_ready=self._codex_ready,
                        ready_providers=self._ready_providers,
                        doctor_checked_at=int(self._readiness_checked_at) if self._readiness_checked_at else None,
                        machine_metrics=machine_metrics,
                    )
                    if isinstance(heartbeat_response, dict):
                        heartbeat_payload = heartbeat_response
                except PullwiseRequestError as exc:
                    self.last_error = f"heartbeat failed: {redact_secrets(str(exc), self.config)}"[:500]
                    loop_error = True
                worker_state = heartbeat_payload.get("worker") if isinstance(heartbeat_payload.get("worker"), dict) else {}
                command = heartbeat_payload.get("command") if isinstance(heartbeat_payload.get("command"), dict) else None
                if worker_state.get("status") == "disabled":
                    ready = False
                if command:
                    ready = False
                    if not running and not loop_error and self.handle_lifecycle_command(command):
                        return
                if ready and free_slots:
                    jobs = []
                    if not loop_error:
                        try:
                            claim_limit = 1 if once else min(free_slots, self.config.max_claim_jobs)
                            jobs = self.client.claim_many(claim_limit)
                        except PullwiseRequestError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
                    if once:
                        jobs = jobs[:1]
                    for job in jobs:
                        future = executor.submit(self.run_job, job)
                        running[future] = job
                    claimed_jobs = len(jobs)
                    if once:
                        concurrent.futures.wait(running)
                        collect_finished_jobs([future for future in running if future.done()])
                        return
                elif once:
                    concurrent.futures.wait(running)
                    collect_finished_jobs([future for future in running if future.done()])
                    return
                sleep_seconds = self.next_poll_sleep(
                    claimed_jobs=claimed_jobs,
                    loop_error=loop_error,
                    free_slots=free_slots,
                )
                if running:
                    concurrent.futures.wait(
                        list(running),
                        timeout=sleep_seconds,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                else:
                    time.sleep(sleep_seconds)

    def machine_metrics_if_due(self) -> dict | None:
        current = time.time()
        if current - self._machine_metrics_checked_at < self.config.machine_metrics_interval_seconds:
            return None
        self._machine_metrics_checked_at = current
        try:
            return worker_machine_metrics_payload(storage_path=str(self.config.work_dir), timestamp=int(current))
        except Exception as exc:
            error = f"machine metrics failed: {redact_secrets(str(exc), self.config)}"
            self.last_error = f"{self.last_error}; {error}"[:500] if self.last_error else error[:500]
            return None

    def cleanup_resources_if_due(self, active_jobs: object, *, force: bool = False) -> None:
        current = time.monotonic()
        if not force and current - self._last_cleanup_at < self.config.cleanup_interval_seconds:
            return
        self._last_cleanup_at = current
        active_job_ids = set()
        for job in active_jobs or []:
            try:
                active_job_ids.add(safe_job_id(job.get("job_id") if isinstance(job, dict) else job))
            except ValueError:
                continue
        try:
            cleanup_worker_resources(self.config, active_job_ids=active_job_ids)
        except Exception as exc:
            self.last_error = f"worker cleanup failed: {redact_secrets(str(exc), self.config)}"[:500]

    def handle_lifecycle_command(self, command: dict) -> bool:
        command_id = str(command.get("id") or "").strip()
        action = str(command.get("command") or "").strip().lower()
        if not command_id or action not in {"stop", "uninstall"}:
            return False
        if getattr(self.config, "lifecycle_watcher_enabled", False):
            return False
        try:
            self.client.command_status(command_id, "running")
        except PullwiseRequestError as exc:
            self.last_error = f"command ack failed: {redact_secrets(str(exc), self.config)}"[:500]
            return False
        code = execute_lifecycle_command(action, self.config) if action == "uninstall" else execute_lifecycle_command(action)
        if code == 0:
            try:
                self.client.command_status(command_id, "succeeded")
            except PullwiseRequestError as exc:
                self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
            return True
        error = f"{action} command exited {code}"
        try:
            self.client.command_status(command_id, "failed", error=error)
        except PullwiseRequestError as exc:
            self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
        return False

    def next_poll_sleep(self, *, claimed_jobs: int, loop_error: bool, free_slots: int | None = None) -> float:
        if loop_error:
            self._error_poll_count += 1
            self._empty_poll_count = 0
            base = self.config.poll_seconds * (2 ** min(self._error_poll_count - 1, 6))
        elif claimed_jobs:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = min(self.config.poll_seconds, 1)
        elif free_slots is not None:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            capacity = max(1, self.config.max_concurrent_jobs)
            if max(0, free_slots) >= capacity:
                base = self.config.poll_seconds
            else:
                base = max(self.config.poll_seconds, min(self.config.max_backoff_seconds, self.config.poll_seconds * 2))
        else:
            self._error_poll_count = 0
            self._empty_poll_count += 1
            base = self.config.poll_seconds * (2 ** min(self._empty_poll_count - 1, 6))
        jitter = random.uniform(0, self.config.poll_jitter_seconds) if self.config.poll_jitter_seconds else 0
        return min(self.config.max_backoff_seconds, base) + jitter

    def refresh_readiness_if_due(self) -> bool:
        current = time.time()
        if current - self._readiness_checked_at < self.config.readiness_check_seconds:
            return self._doctor_status == "ok"
        checks, _provider_ready, ready_providers = worker_readiness_state(self.config)
        failed_check = first_failed_check(checks)
        if (
            failed_check is not None
            and failed_check[0] == "codex_ready"
            and "deferred" in str(failed_check[2] or "").lower()
            and (self._doctor_status == "ok" or self._codex_ready or self._ready_providers)
        ):
            self._readiness_checked_at = current
            return True
        self._codex_ready = readiness_check_ok(checks, "codex_ready")
        self._ready_providers = ready_providers
        self._doctor_status = "degraded" if failed_check else "ok"
        self._readiness_checked_at = current
        self.last_error = None if failed_check is None else readiness_error_message(failed_check, self.config)
        return failed_check is None

    def upload_result_with_retry(self, job_id: str, payload: dict) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.result_upload_attempts + 1):
            try:
                self.client.result(job_id, payload)
                return
            except PullwiseHTTPError as exc:
                if exc.status_code < 500 or attempt >= self.config.result_upload_attempts:
                    raise
                last_error = exc
            except PullwiseRequestError as exc:
                last_error = exc
                if attempt >= self.config.result_upload_attempts:
                    raise
            if attempt < self.config.result_upload_attempts:
                time.sleep(min(30, 2 ** (attempt - 1)))
        if last_error:
            raise last_error

    def run_job(self, job: dict) -> None:
        job_id = safe_job_id(job.get("job_id"))
        job_config = self.config
        configured_agent = {}
        attempt_id = f"{self.config.worker_id}-{job.get('attempt') or 1}"
        checkout_dir = checkout_dir_for_job(self.config.work_dir, job_id)
        started = time.monotonic()
        duration_ms = 0
        job_error = ""
        current_stage = "clone"
        resolved_commit = ""
        preflight: dict = {}
        graph_verified_report: dict = {}
        candidate_count = 0
        rejected_reasons: dict[str, int] = {}
        review_execution: dict = {}
        logs_summary = ""
        job_trace_checkpoints: list[dict] = []

        def current_job_trace(result_status: str = "running", next_retry_hint: str = "") -> dict:
            return job_trace_payload(
                result_status=result_status,
                checkpoints=job_trace_checkpoints,
                candidate_count_before_filter=candidate_count,
                rejected_reasons=rejected_reasons,
                next_retry_hint=next_retry_hint,
            )

        def scan_summary_review_execution() -> dict:
            return review_execution if isinstance(review_execution, dict) else {}

        def completion_error_detail(base_error: str) -> str:
            parts = [protocol_multiline_text(base_error) or "Worker completion audit failed."]
            timing_summary = codex_review_execution_summary(review_execution)
            if timing_summary:
                parts.append(timing_summary)
            log_excerpt = protocol_multiline_text(logs_summary)
            if log_excerpt:
                parts.append(f"logs: {log_excerpt[-500:]}")
            return redact_secrets("; ".join(part for part in parts if part), job_config)[:1200]

        try:
            job_config = worker_config_for_job(self.config, job)
            configured_agent = effective_agent_config_payload(job_config)
            graph_verified_review_enabled(job_config, job)
            checkout_dir = checkout_dir_for_job(job_config.work_dir, job_id)
            self.client.progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Cloning repository",
            )
            resolved_commit = clone_repository(job, checkout_dir)
            job["resolved_commit"] = resolved_commit
            job["commit"] = resolved_commit
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "clone",
                    summary="Repository cloned.",
                    details={"commit": resolved_commit[:12], "repo": job.get("repo")},
                )
            )
            self.client.progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Repository cloned",
            )
            current_stage = "preflight"
            limit_preflight = enforce_repository_limits(job_config, job, checkout_dir)
            self.client.progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository ready",
            )
            preflight = collect_preflight_metadata(job_config, job, checkout_dir)
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "preflight",
                    summary=protocol_multiline_text(preflight.get("summary")) or "Repository preflight collected.",
                    details={
                        "mode": preflight.get("mode"),
                        "execution": preflight.get("execution"),
                        "languages": preflight.get("languages"),
                        "packageManagers": preflight.get("packageManagers"),
                        "repositoryStats": limit_preflight.get("repositoryStats"),
                    },
                )
            )
            self.client.progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository preflight ready",
            )
            current_stage = "graph"
            graph_summary = "GraphVerified will build graph slices during review."
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "graph",
                    summary=graph_summary,
                    details={"source": "codereview-slices"},
                )
            )
            self.client.progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                graph_summary,
            )
            current_stage = "verifier"
            try:
                verifier, verifier_findings, verifier_logs = run_verifier_commands(job_config, job, checkout_dir, preflight)
            except Exception as exc:
                verifier = {
                    "enabled": job_config.verifier_enabled,
                    "summary": f"Verifier failed before completing: {redact_secrets(str(exc), job_config)}"[:500],
                    "runs": [],
                }
                verifier_findings = []
                verifier_logs = verifier["summary"]
            preflight["verifier"] = verifier
            verifier_runs = verifier.get("runs") if isinstance(verifier.get("runs"), list) else []
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "verifier",
                    status="warning" if protocol_multiline_text(verifier.get("summary")).startswith("Verifier failed") else "ok",
                    summary=protocol_multiline_text(verifier.get("summary")) or "Verifier stage completed.",
                    counts={"runs": len(verifier_runs), "findings": len(verifier_findings)},
                    details={"enabled": verifier.get("enabled") is True},
                    logs_summary=verifier_logs,
                )
            )
            if verifier.get("enabled") and verifier.get("runs"):
                preflight["execution"] = "allowlisted_verifier_scripts"
                preflight["summary"] = (
                    "Static preflight captured repository metadata, then the verifier executed allowlisted "
                    "setup and project check commands with bounded timeouts and logs."
                )
            self.client.progress(
                job_id,
                "ai",
                PHASE_PROGRESS["ai"],
                "Running GraphVerified review",
                verifier_logs,
            )
            current_stage = "graph_verified"
            graph_verified_report = run_graph_verified_review_payload(
                job_config,
                job,
                checkout_dir,
                resolved_commit or "HEAD",
            )
            projected_findings = graph_verified_summary_findings(graph_verified_report)
            summary = summarize(projected_findings)
            logs_summary = protocol_multiline_text(graph_verified_report.get("debugMarkdown"))[-1000:]
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "graph_verified",
                    status="warning" if graph_verified_report.get("blockedCount") else "ok",
                    summary="GraphVerified confirmed-only review completed.",
                    counts={
                        "confirmed": graph_verified_report.get("confirmedCount"),
                        "rejected": graph_verified_report.get("rejectedCount"),
                        "blocked": graph_verified_report.get("blockedCount"),
                    },
                    details={
                        "runId": graph_verified_report.get("runId"),
                        "mode": graph_verified_report.get("mode"),
                        "source": "confirmed-only-report",
                    },
                    logs_summary=logs_summary,
                )
            )
            effective_agent_config = configured_agent
            ai_usage = normalize_ai_usage({})
            candidate_count = len(projected_findings)
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "agent",
                    summary=(
                        "GraphVerified confirmed-only report normalized."
                    ),
                    counts={
                        "confirmed": graph_verified_report.get("confirmedCount"),
                        "rejected": graph_verified_report.get("rejectedCount"),
                        "blocked": graph_verified_report.get("blockedCount"),
                        "candidateCount": candidate_count,
                    },
                    details={
                        "provider": "graph-verified",
                        "model": ai_usage.get("model"),
                    },
                    logs_summary=logs_summary,
                )
            )
            self.client.progress(
                job_id,
                "ai",
                PHASE_PROGRESS["ai"],
                "GraphVerified review complete",
                logs_summary,
            )
            current_stage = "filter"
            rejected_reasons = {}
            summary = summarize(projected_findings)
            if verifier_logs:
                logs_summary = "\n".join([verifier_logs, logs_summary])[-1000:]
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "filter",
                    summary="GraphVerified confirmed-only report accepted for reporting.",
                    counts={
                        "candidateCountBeforeFilter": candidate_count,
                        "confirmed": graph_verified_report.get("confirmedCount"),
                        "rejected": graph_verified_report.get("rejectedCount"),
                        "blocked": graph_verified_report.get("blockedCount"),
                    },
                    details={
                        "rejectionReasons": [
                            {"reason": reason, "count": count}
                            for reason, count in sorted(rejected_reasons.items())
                            if count > 0
                        ]
                    },
                )
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            current_stage = "report"
            completion_audit = graph_verified_completion_payload(graph_verified_report, result_status="done")
            result_status = "done"
            completion_error = ""
            if (
                graph_verified_report.get("blockedCount")
                and not graph_verified_report.get("runId")
            ):
                result_status = "failed"
                completion_error = (
                    protocol_multiline_text(graph_verified_report.get("debugMarkdown"))
                    or "GraphVerified review failed before producing a run report."
                )
            completion_blockers = completion_audit.get("blockers") if isinstance(completion_audit.get("blockers"), list) else []
            completion_blocker_text = "; ".join(protocol_multiline_text(item) for item in completion_blockers if protocol_multiline_text(item))
            if completion_blocker_text:
                completion_error = completion_error_detail(completion_blocker_text)
            result_summary = summary
            if result_status == "failed":
                result_summary = summarize([])
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    "report",
                    status=(
                        "failed"
                        if completion_audit.get("status") == "failed"
                        else "warning" if completion_audit.get("status") == "warning" else "ok"
                    ),
                    summary=completion_audit.get("summary") or "Worker result payload is ready.",
                    counts={
                        "confirmed": graph_verified_report.get("confirmedCount"),
                        "rejected": graph_verified_report.get("rejectedCount"),
                        "blocked": graph_verified_report.get("blockedCount"),
                    },
                )
            )
            job_trace = job_trace_payload(
                result_status=result_status,
                checkpoints=job_trace_checkpoints,
                candidate_count_before_filter=candidate_count,
                rejected_reasons=rejected_reasons,
                next_retry_hint=completion_audit.get("retryReason") if completion_audit.get("retryRecommended") else "",
            )
            payload = {
                "status": result_status,
                "commit": resolved_commit,
                "resolved_commit": resolved_commit,
                "summary": result_summary,
                "duration_ms": duration_ms,
                "attempt_id": attempt_id,
                "preflight": preflight,
                "effectiveAgentConfig": effective_agent_config,
                "graphVerifiedReport": graph_verified_report,
            }
            if result_status == "failed":
                payload["error"] = completion_error or "Worker completion audit failed."
                payload["error_code"] = "COMPLETION_AUDIT_FAILED"
                payload["errorCode"] = "COMPLETION_AUDIT_FAILED"
            if ai_usage:
                payload["aiUsage"] = ai_usage
            payload["result_checksum"] = result_checksum(payload)
            self.client.progress(
                job_id,
                "report",
                100,
                "Uploading result",
                logs_summary,
            )
            try:
                self.upload_result_with_retry(job_id, payload)
            except Exception as exc:
                self.last_error = f"result upload failed for {job_id}: {redact_secrets(str(exc), job_config)}"[:500]
                job_error = self.last_error
                write_scan_summary(
                    job_config,
                    job_id,
                    "upload_failed",
                    duration_ms,
                    self.last_error,
                    scan_summary_review_execution(),
                )
                return
            if result_status == "failed":
                job_error = payload["error"]
                self.last_error = payload["error"]
                write_scan_summary(
                    job_config,
                    job_id,
                    "failed",
                    duration_ms,
                    payload["error"],
                    scan_summary_review_execution(),
                )
            else:
                write_scan_summary(job_config, job_id, "done", duration_ms, "", scan_summary_review_execution())
                self.last_error = None
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            raw_review_execution = getattr(exc, "review_execution", None)
            if isinstance(raw_review_execution, dict) and raw_review_execution:
                review_execution = raw_review_execution
            error = redact_secrets(str(exc), job_config)[:1200]
            error_code = str(getattr(exc, "error_code", "") or "").strip()
            error_preflight = getattr(exc, "preflight", None)
            if not isinstance(error_preflight, dict):
                error_preflight = {}
            failure_preflight = error_preflight or preflight
            agent_config = job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}
            graph_config = agent_config.get("graphVerified") if isinstance(agent_config.get("graphVerified"), dict) else {}
            graph_mode = protocol_multiline_text(graph_config.get("mode")) or "standard"
            graph_verified_report = graph_verified_report or {
                "version": "graph-verified-code-review/1",
                "mode": graph_mode,
                "base": protocol_multiline_text(job.get("base_commit") or job.get("baseCommit")) or "",
                "head": resolved_commit or "HEAD",
                "confirmedCount": 0,
                "rejectedCount": 0,
                "blockedCount": 1,
                "debugMarkdown": f"Graph-verified review failed before confirmation: {error}",
                "finalJson": {"confirmed": []},
            }
            job_trace_checkpoints.append(
                job_trace_checkpoint(
                    current_stage,
                    status="failed",
                    summary=error,
                    details={"errorCode": error_code},
                )
            )
            error_payload = {
                "status": "failed",
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": duration_ms,
                "error": error,
                "attempt_id": attempt_id,
                "graphVerifiedReport": graph_verified_report,
            }
            if error_code:
                error_payload["error_code"] = error_code
                error_payload["errorCode"] = error_code
            if failure_preflight:
                error_payload["preflight"] = failure_preflight
            if configured_agent:
                error_payload["effectiveAgentConfig"] = configured_agent
            if resolved_commit:
                error_payload["commit"] = resolved_commit
                error_payload["resolved_commit"] = resolved_commit
            error_payload["result_checksum"] = result_checksum(error_payload)
            try:
                self.client.progress(
                    job_id,
                    "report",
                    PHASE_PROGRESS["report"],
                    "Uploading failed result",
                    error,
                )
                self.upload_result_with_retry(job_id, error_payload)
            except Exception as upload_exc:
                self.last_error = f"failed result upload failed for {job_id}: {redact_secrets(str(upload_exc), job_config)}"[:500]
                job_error = self.last_error
                write_scan_summary(
                    job_config,
                    job_id,
                    "upload_failed",
                    duration_ms,
                    self.last_error,
                    scan_summary_review_execution(),
                )
                return
            write_scan_summary(job_config, job_id, "failed", duration_ms, error, scan_summary_review_execution())
            self.last_error = error
            job_error = error
        finally:
            if job_error and job_config.failed_checkout_retention_seconds > 0:
                marker = failed_checkout_marker(checkout_dir)
                marker.write_text(str(int(time.time()) + job_config.failed_checkout_retention_seconds), encoding="utf-8")
            else:
                shutil.rmtree(checkout_dir, ignore_errors=True)


def safe_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not job_id or job_id in {".", ".."} or not _SAFE_JOB_ID_RE.match(job_id):
        raise ValueError("job_id contains unsafe path characters")
    return job_id


def checkout_dir_for_job(work_dir: Path, job_id: str) -> Path:
    root = work_dir.resolve(strict=False)
    checkout_dir = (work_dir / safe_job_id(job_id)).resolve(strict=False)
    try:
        common = os.path.commonpath([str(root), str(checkout_dir)])
    except ValueError as exc:
        raise ValueError("job checkout directory must stay inside work_dir") from exc
    if os.path.normcase(common) != os.path.normcase(str(root)) or checkout_dir == root:
        raise ValueError("job checkout directory must stay inside work_dir")
    return checkout_dir


def failed_checkout_marker(checkout_dir: Path) -> Path:
    return checkout_dir.parent / f"{checkout_dir.name}{_FAILED_CHECKOUT_MARKER_SUFFIX}"


def checkout_dir_from_failed_marker(marker: Path) -> Path:
    name = marker.name
    if not name.endswith(_FAILED_CHECKOUT_MARKER_SUFFIX):
        return marker.with_suffix("")
    return marker.parent / name[: -len(_FAILED_CHECKOUT_MARKER_SUFFIX)]


def checkout_root_sentinel(work_dir: Path) -> Path:
    return work_dir / _CHECKOUT_ROOT_SENTINEL_NAME


def checkout_root_is_owned(work_dir: Path) -> bool:
    sentinel = checkout_root_sentinel(work_dir)
    if sentinel.is_file():
        return True
    entries = [path for path in work_dir.iterdir() if path.name not in _CHECKOUT_RUNTIME_DIR_NAMES]
    if entries:
        return False
    try:
        sentinel.write_text("pullwise-worker checkout root\n", encoding="utf-8")
    except OSError:
        return False
    return True


def remove_checkout_dir(checkout_dir: Path) -> None:
    if checkout_dir.is_symlink():
        raise RuntimeError(f"Refusing to remove symlinked checkout directory: {checkout_dir}")
    if not checkout_dir.exists():
        return

    def retry_readonly_remove(function, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
        function(path)

    shutil.rmtree(checkout_dir, onerror=retry_readonly_remove)
    if checkout_dir.exists():
        raise RuntimeError(f"Failed to remove previous checkout directory: {checkout_dir}")


def clone_repository(job: dict, checkout_dir: Path) -> str:
    remove_checkout_dir(checkout_dir)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_token = job.get("clone_token")
    clone_url = str(job.get("clone_url") or "")
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = f"{worker_github_web_url()}/{repo}.git"
    if clone_token_value(clone_token):
        clone_url = trusted_clone_url_for_token(job, clone_url, clone_token)
    git_env = git_auth_env(clone_token, clone_url, job.get("repo"))
    commit = str(job.get("commit") or "pending")
    clone_command = ["git", "clone"]
    if not commit or commit == "pending":
        clone_command.extend(["--depth", "1"])
    clone_command.extend(["--branch", str(job.get("branch") or "main"), clone_url, str(checkout_dir)])
    run_git_command(clone_command, phase="clone", env=git_env)
    if commit and commit != "pending":
        run_git_command(
            ["git", "-C", str(checkout_dir), "checkout", commit],
            phase="checkout",
        )
    return resolve_git_head(checkout_dir)


def resolve_git_head(checkout_dir: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(git_error_message("rev-parse", exc)) from exc
    commit = (completed.stdout or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("git rev-parse HEAD did not return a commit SHA")
    return commit.lower()


def run_git_command(command: list[str], *, phase: str, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {phase} timed out after {env_int('PULLWISE_GIT_TIMEOUT_SECONDS', 600)}s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(git_error_message(phase, exc)) from exc


def git_error_message(phase: str, exc: subprocess.CalledProcessError) -> str:
    output = "\n".join(part for part in (exc.stderr, exc.stdout) if part)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " ".join(lines[:3])[:400]
    if not summary:
        summary = f"git exited with status {exc.returncode}"
    return f"git {phase} failed: {summary}"


def clone_token_value(clone_token: object) -> str:
    token = clone_token.get("token") if isinstance(clone_token, dict) else None
    return str(token or "")


_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def worker_github_web_url() -> str:
    raw = (
        os.environ.get("PULLWISE_GITHUB_WEB_URL")
        or os.environ.get("GITHUB_WEB_URL")
        or "https://github.com"
    ).strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError("PULLWISE_GITHUB_WEB_URL must be an absolute GitHub web HTTP(S) origin.")
    if parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("PULLWISE_GITHUB_WEB_URL must be an absolute GitHub web HTTP(S) origin.")
    return raw


def validate_repo_full_name(repo: object) -> str:
    value = str(repo or "").strip()
    if not _REPO_FULL_NAME_RE.fullmatch(value):
        raise RuntimeError("Repository must be a GitHub full name like owner/repo.")
    return value


def clone_token_repo(clone_token: object) -> str:
    repo = clone_token.get("repo") if isinstance(clone_token, dict) else None
    return str(repo or "").strip()


def expected_clone_repo(job: dict | None, clone_token: object) -> str:
    job_repo = str(job.get("repo") or "").strip() if isinstance(job, dict) else ""
    token_repo = clone_token_repo(clone_token)
    if job_repo:
        job_repo = validate_repo_full_name(job_repo)
    if token_repo:
        token_repo = validate_repo_full_name(token_repo)
    if job_repo and token_repo and job_repo.lower() != token_repo.lower():
        raise RuntimeError("Clone token repository does not match job repository.")
    return token_repo or validate_repo_full_name(job_repo)


def trusted_clone_url_for_repo(repo: object, clone_url: object) -> str:
    repo_name = validate_repo_full_name(repo)
    if clone_url is None or clone_url == "":
        clone_url = f"{worker_github_web_url()}/{repo_name}.git"
    if not isinstance(clone_url, str):
        raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
    clone_url = clone_url.strip()
    if not clone_url or any(char in clone_url for char in "\r\n"):
        raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
    parsed = urllib.parse.urlparse(clone_url)
    allowed = urllib.parse.urlparse(worker_github_web_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Repository clone URL must be an HTTP(S) URL.")
    if parsed.netloc.lower() != allowed.netloc.lower():
        raise RuntimeError("Repository clone URL host does not match configured GitHub host.")
    if parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("Repository clone URL must not include query or fragment.")
    clone_path = parsed.path.rstrip("/")
    expected_path = clone_path[:-4] if clone_path.lower().endswith(".git") else clone_path
    if expected_path.lower() != f"/{repo_name.lower()}":
        raise RuntimeError("Repository clone URL path does not match requested repository.")
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), clone_path, "", "", ""))


def trusted_clone_url_for_token(job: dict | None, clone_url: object, clone_token: object) -> str:
    return trusted_clone_url_for_repo(expected_clone_repo(job, clone_token), clone_url)


def git_extra_header_key(clone_url: str) -> str:
    return f"http.{clone_url.rstrip('/')}.extraHeader"


def git_auth_env(clone_token: object, clone_url: object = None, repo: object = None) -> dict[str, str] | None:
    token = clone_token_value(clone_token)
    if not token:
        return None
    scoped_clone_url = trusted_clone_url_for_token({"repo": repo}, clone_url, clone_token)
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": git_extra_header_key(scoped_clone_url),
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    )
    return env


_README_PACKAGE_SCRIPT_RE = re.compile(r"\b(npm|pnpm|yarn|bun)\s+run\s+([A-Za-z0-9:_-]+)\b")
_HIGH_SIGNAL_PACKAGE_SCRIPTS = {"dev", "start", "build", "test"}
_PACKAGE_SCRIPT_NAMES = ["dev", "start", "build", "test", "lint", "typecheck", "check"]
_VERIFIER_DEFAULT_SCRIPTS = ["build", "test", "lint", "typecheck", "check"]
_VERIFIER_DISABLED_VALUES = {"", "0", "false", "no", "off"}
_VERIFIER_MAX_OUTPUT_CHARS = 4000
_VERIFIER_OUTPUT_WITHHELD = "Verifier stdout/stderr is withheld from API responses and audit bundles."
_VERIFIER_ENV_PASSTHROUGH_KEYS = (
    "PATH",
    "Path",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "ComSpec",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
_VERIFICATION_STATUSES = {"verified", "static_proof", "potential_risk", "unverified"}
_LOCKFILE_PACKAGE_MANAGERS = {
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}
_MANIFEST_TYPES = {
    "Cargo.toml": "rust",
    "Pipfile": "python",
    "go.mod": "go",
    "package.json": "node",
    "poetry.lock": "python-lock",
    "pyproject.toml": "python",
    "requirements.txt": "python",
}
_CONFIG_MANIFEST_TYPES = {
    ".devcontainer/devcontainer.json": "devcontainer",
    "compose.yaml": "docker-compose",
    "compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "docker-compose.yml": "docker-compose",
    "Dockerfile": "dockerfile",
}
_DOCKERFILE_SCAN_MAX_FILES = 50
_DOCKERFILE_SKIP_DIRS = {
    ".git",
    "docs",
    "examples",
    "fixtures",
    "node_modules",
    "test",
    "tests",
    "vendor",
    "__tests__",
}
_SECRET_SCAN_MAX_BYTES = 256 * 1024
_SECRET_SCAN_MAX_FILES = 3000
_SECRET_SCAN_SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".tox",
    ".venv",
    "build",
    "coverage",
    "dist",
    "docs",
    "examples",
    "fixtures",
    "node_modules",
    "target",
    "test",
    "tests",
    "vendor",
    "venv",
    "__pycache__",
    "__tests__",
}
_SECRET_SCAN_SKIP_FILES = {
    "bun.lock",
    "bun.lockb",
    "Cargo.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
_SECRET_SCAN_TEXT_SUFFIXES = {
    ".bash",
    ".conf",
    ".config",
    ".cs",
    ".env",
    ".go",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".php",
    ".properties",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".zsh",
}
_SECRET_PATTERNS = [
    {
        "kind": "github_token",
        "label": "GitHub token",
        "regex": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,255}\b"),
    },
    {
        "kind": "slack_token",
        "label": "Slack token",
        "regex": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    },
]
