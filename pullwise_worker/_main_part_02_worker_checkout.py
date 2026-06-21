from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

AGENT_REASONING_LEVELS = {"low", "medium", "high", "xhigh"}
AGENT_CONFIG_TEXT_MAX_LENGTH = 128
PROTOCOL_TEXT_MAX_LENGTH = 4000
PROTOCOL_SINGLE_LINE_TEXT_MAX_LENGTH = 500


def protocol_multiline_text(value: object, max_length: int = PROTOCOL_TEXT_MAX_LENGTH) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    try:
        limit = max(0, int(max_length))
    except (TypeError, ValueError, OverflowError):
        limit = PROTOCOL_TEXT_MAX_LENGTH
    if limit <= 0:
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def clean_protocol_text(value: object, max_length: int = PROTOCOL_SINGLE_LINE_TEXT_MAX_LENGTH) -> str:
    return protocol_multiline_text(value, max_length).split("\n", 1)[0].strip()


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


def graph_verified_completion_error(report: dict | None) -> str:
    if not isinstance(report, dict) or not report:
        return "GraphVerified review did not produce a report."
    blocked_count = graph_verified_report_int(report.get("blockedCount"))
    if blocked_count and not report.get("runId"):
        return (
            protocol_multiline_text(report.get("debugMarkdown"))
            or "GraphVerified review failed before producing a run report."
        )

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    finder = summary.get("finder") if isinstance(summary.get("finder"), dict) else {}
    candidates = summary.get("candidates") if isinstance(summary.get("candidates"), dict) else {}
    reports = summary.get("reports") if isinstance(summary.get("reports"), dict) else {}
    finder_results = graph_verified_report_int(finder.get("results"))
    finder_blocked = graph_verified_report_int(finder.get("blocked"))
    finder_candidates = graph_verified_report_int(finder.get("candidates"))
    valid_candidates = graph_verified_report_int(candidates.get("valid"))
    selected_for_repro = graph_verified_report_int(candidates.get("selectedForRepro"))
    confirmed = graph_verified_report_int(report.get("confirmedCount")) or graph_verified_report_int(reports.get("confirmed"))
    rejected = graph_verified_report_int(report.get("rejectedCount")) or graph_verified_report_int(reports.get("rejected"))
    blocked_total = blocked_count or graph_verified_report_int(reports.get("blocked"))

    no_reportable_work = (
        confirmed == 0
        and rejected == 0
        and finder_candidates == 0
        and valid_candidates == 0
        and selected_for_repro == 0
    )
    if finder_results > 0 and finder_blocked >= finder_results and no_reportable_work:
        return graph_verified_blocked_completion_message(
            "GraphVerified finder pipeline blocked every finder task before producing candidates",
            graph_verified_first_blocked_reason(report),
        )
    if blocked_total > 0 and no_reportable_work:
        return graph_verified_blocked_completion_message(
            "GraphVerified review blocked before producing reportable candidates",
            graph_verified_first_blocked_reason(report),
        )
    return ""


def graph_verified_report_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def graph_verified_first_blocked_reason(report: dict) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    for section_name in ("finder", "repro", "judge"):
        section = summary.get(section_name) if isinstance(summary.get(section_name), dict) else {}
        blocked_items = section.get("blockedItems")
        if not isinstance(blocked_items, list):
            continue
        for item in blocked_items:
            if not isinstance(item, dict):
                continue
            reason = clean_protocol_text(item.get("reason"), 700)
            if reason:
                return reason
    return clean_protocol_text(report.get("debugMarkdown"), 700)


def graph_verified_blocked_completion_message(message: str, reason: str) -> str:
    reason_text = clean_protocol_text(reason, 700)
    return f"{message}: {reason_text}" if reason_text else message


def log_graph_verified_completion(job_id: str, attempt_id: str, status: str, report: dict | None, error: str = "") -> None:
    summary = report.get("summary") if isinstance(report, dict) and isinstance(report.get("summary"), dict) else {}
    finder = summary.get("finder") if isinstance(summary.get("finder"), dict) else {}
    candidates = summary.get("candidates") if isinstance(summary.get("candidates"), dict) else {}
    fields = [
        "pullwise_worker graph_verified completion",
        f"job_id={clean_protocol_text(job_id)}",
        f"attempt_id={clean_protocol_text(attempt_id)}",
        f"status={clean_protocol_text(status)}",
        f"blocked={graph_verified_report_int((report or {}).get('blockedCount') if isinstance(report, dict) else 0)}",
        f"finder_tasks={graph_verified_report_int(finder.get('tasks'))}",
        f"finder_blocked={graph_verified_report_int(finder.get('blocked'))}",
        f"finder_candidates={graph_verified_report_int(finder.get('candidates'))}",
        f"valid_candidates={graph_verified_report_int(candidates.get('valid'))}",
    ]
    error_text = clean_protocol_text(error, 500)
    if error_text:
        fields.append(f"error={error_text}")
    print(" ".join(fields), file=sys.stderr, flush=True)


def summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = graph_verified_severity(finding.get("severity"))
        summary[severity] += 1
    return summary


def normalize_ai_usage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    usage = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            usage[key] = item
        elif isinstance(item, dict):
            nested = normalize_ai_usage(item)
            if nested:
                usage[key] = nested
    return usage


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
        self._cleanup_future: concurrent.futures.Future[None] | None = None
        self._cleanup_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pullwise-cleanup",
        )
        self._result_upload_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pullwise-result-upload",
        )
        self._pending_result_uploads: dict[str, tuple[concurrent.futures.Future[None], Path]] = {}
        self._pending_result_uploads_lock = Lock()
        self.log_tailers: dict[str, WorkerLogStreamTailer] = {}

    def handle_log_session(self, session: object) -> None:
        session_id = log_stream_session_id(session if isinstance(session, dict) else None)
        if not session_id:
            self.log_tailers.clear()
            return
        if session_id not in self.log_tailers:
            self.log_tailers = {session_id: WorkerLogStreamTailer(self.config, session)}
        tailer = self.log_tailers[session_id]
        entries, state = tailer.collect()
        if not entries:
            return
        try:
            self.client.log_stream_lines(session_id, entries[:500])
        except PullwiseRequestError as exc:
            self.last_error = f"log stream upload failed: {redact_secrets(str(exc), self.config)}"[:500]
            return
        tailer.commit(state)

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.load_pending_result_uploads()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            running_future: concurrent.futures.Future[None] | None = None
            running_job: dict | None = None

            def collect_finished_job() -> None:
                nonlocal running_future, running_job
                if running_future is None or not running_future.done():
                    return
                future = running_future
                job = running_job or {}
                running_future = None
                running_job = None
                try:
                    future.result()
                except Exception as exc:
                    self.last_error = f"job {job.get('job_id')} failed unexpectedly: {exc}"[:500]

            while True:
                collect_finished_job()
                self.collect_result_uploads()
                self.collect_cleanup()
                job_running = running_future is not None
                ready = self.refresh_readiness_if_due()
                loop_error = False
                claimed_jobs = 0
                heartbeat_payload: dict = {}
                machine_metrics = self.machine_metrics_if_due()
                active_job_ids = self.pending_result_job_ids()
                if running_job is not None:
                    try:
                        active_job_id = safe_job_id(running_job.get("job_id"))
                        if active_job_id not in active_job_ids:
                            active_job_ids.append(active_job_id)
                    except ValueError:
                        pass
                try:
                    heartbeat_response = self.client.heartbeat(
                        running_jobs=1 if job_running else 0,
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
                if not getattr(self.config, "lifecycle_watcher_enabled", False):
                    self.handle_log_session(heartbeat_payload.get("logSession") or heartbeat_payload.get("log_session"))
                if worker_state.get("status") == "disabled":
                    ready = False
                if command:
                    ready = False
                    if not job_running and not loop_error and self.handle_lifecycle_command(command):
                        return
                if self.cleanup_is_running():
                    ready = False
                if ready and not job_running:
                    job = None
                    if not loop_error:
                        try:
                            job = self.client.claim()
                        except PullwiseRequestError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
                    if job:
                        running_job = job
                        running_future = executor.submit(self.run_job, job)
                    claimed_jobs = 1 if job else 0
                    if once:
                        if running_future is not None:
                            concurrent.futures.wait([running_future])
                        collect_finished_job()
                        self.collect_result_uploads()
                        return
                elif once:
                    if running_future is not None:
                        concurrent.futures.wait([running_future])
                    collect_finished_job()
                    self.collect_result_uploads()
                    return
                if running_future is None and not claimed_jobs and not loop_error:
                    self.schedule_cleanup_resources_if_due(active_job_ids)
                sleep_seconds = self.next_poll_sleep(
                    claimed_jobs=claimed_jobs,
                    loop_error=loop_error,
                    worker_busy=running_future is not None,
                )
                if running_future is not None:
                    concurrent.futures.wait(
                        [running_future],
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

    def cleanup_is_running(self) -> bool:
        return bool(self._cleanup_future and not self._cleanup_future.done())

    def collect_cleanup(self) -> None:
        if not self._cleanup_future or not self._cleanup_future.done():
            return
        future = self._cleanup_future
        self._cleanup_future = None
        try:
            future.result()
        except Exception as exc:
            self.last_error = f"worker cleanup failed: {redact_secrets(str(exc), self.config)}"[:500]

    def schedule_cleanup_resources_if_due(self, active_jobs: object, *, force: bool = False) -> None:
        if self.cleanup_is_running():
            return
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
        self._cleanup_future = self._cleanup_executor.submit(
            cleanup_worker_resources,
            self.config,
            active_job_ids=active_job_ids,
        )

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

    def next_poll_sleep(self, *, claimed_jobs: int, loop_error: bool, worker_busy: bool = False) -> float:
        if loop_error:
            self._error_poll_count += 1
            self._empty_poll_count = 0
            base = self.config.poll_seconds * (2 ** min(self._error_poll_count - 1, 6))
        elif claimed_jobs:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = min(self.config.poll_seconds, 1)
        elif worker_busy:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = max(self.config.poll_seconds, min(self.config.max_backoff_seconds, self.config.poll_seconds * 2))
        else:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = self.config.poll_seconds
        jitter = random.uniform(0, self.config.poll_jitter_seconds) if self.config.poll_jitter_seconds else 0
        return min(self.config.max_backoff_seconds, base) + jitter

    def refresh_readiness_if_due(self) -> bool:
        current = time.time()
        if current - self._readiness_checked_at < self.config.readiness_check_seconds:
            return self._doctor_status == "ok"
        try:
            checks, _provider_ready, ready_providers = worker_readiness_state(self.config)
        except Exception as exc:
            self._codex_ready = False
            self._ready_providers = []
            self._doctor_status = "degraded"
            self._readiness_checked_at = current
            self.last_error = f"worker not ready: readiness check failed: {redact_secrets(str(exc), self.config)}"[:500]
            return False
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

    def load_pending_result_uploads(self) -> None:
        pending_dir = result_upload_dir(self.config.work_dir)
        if not pending_dir.exists():
            return
        for path in sorted(pending_dir.glob("*.json")):
            job_id = path.stem
            try:
                job_id = safe_job_id(job_id)
            except ValueError:
                continue
            self.schedule_pending_result_upload(job_id, path)

    def pending_result_job_ids(self) -> list[str]:
        with self._pending_result_uploads_lock:
            return [
                job_id
                for job_id, (future, _path) in self._pending_result_uploads.items()
                if not future.done()
            ]

    def collect_result_uploads(self) -> None:
        done_uploads: list[tuple[str, concurrent.futures.Future[None], Path]] = []
        with self._pending_result_uploads_lock:
            for job_id, (future, path) in list(self._pending_result_uploads.items()):
                if future.done():
                    done_uploads.append((job_id, future, path))
                    self._pending_result_uploads.pop(job_id, None)
        for job_id, future, path in done_uploads:
            try:
                future.result()
            except PullwiseHTTPError as exc:
                if exc.status_code < 500:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.last_error = (
                        f"result upload permanently failed for {job_id}: "
                        f"{redact_secrets(str(exc), self.config)}"
                    )[:500]
                    continue
                self.last_error = f"result upload retry failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                self.schedule_pending_result_upload(job_id, path)
            except PullwiseRequestError as exc:
                self.last_error = f"result upload retry failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                self.schedule_pending_result_upload(job_id, path)
            except Exception as exc:
                self.last_error = f"result upload failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                self.schedule_pending_result_upload(job_id, path)

    def schedule_pending_result_upload(self, job_id: str, path: Path) -> None:
        job_id = safe_job_id(job_id)
        with self._pending_result_uploads_lock:
            current = self._pending_result_uploads.get(job_id)
            if current and not current[0].done():
                return
            future = self._result_upload_executor.submit(self.upload_pending_result_file, path)
            self._pending_result_uploads[job_id] = (future, path)

    def upload_pending_result_file(self, path: Path) -> None:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise PullwiseRequestError("pending result upload record must be an object")
        job_id = safe_job_id(record.get("job_id"))
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise PullwiseRequestError("pending result upload payload must be an object")
        self.upload_result_with_retry(job_id, payload)
        path.unlink(missing_ok=True)

    def defer_result_upload(self, job_id: str, payload: dict) -> Path:
        pending_dir = result_upload_dir(self.config.work_dir)
        pending_dir.mkdir(parents=True, exist_ok=True)
        path = result_upload_file(self.config.work_dir, job_id)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(
                {
                    "job_id": safe_job_id(job_id),
                    "created_at": int(time.time()),
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temp_path.replace(path)
        self.schedule_pending_result_upload(job_id, path)
        return path

    def upload_result_once_or_defer(self, job_id: str, payload: dict) -> bool:
        try:
            self.client.result(job_id, payload)
            return True
        except PullwiseHTTPError as exc:
            if exc.status_code < 500:
                raise
            error = exc
        except PullwiseRequestError as exc:
            error = exc
        self.defer_result_upload(job_id, payload)
        self.last_error = f"result upload deferred for {job_id}: {redact_secrets(str(error), self.config)}"[:500]
        return False

    def report_progress(
        self,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
        *,
        config: WorkerConfig | None = None,
    ) -> bool:
        active_config = config or self.config
        try:
            write_scan_progress_summary(active_config, job_id, phase, progress, message, logs_summary)
        except Exception:
            pass
        try:
            self.client.progress(job_id, phase, progress, message, logs_summary)
            return True
        except Exception as exc:
            self.last_error = (
                f"progress update failed for {job_id} at {phase}: "
                f"{redact_secrets(str(exc), active_config)}"
            )[:500]
            return False

    def run_job(self, job: dict) -> None:
        job_id = safe_job_id(job.get("job_id"))
        job_config = self.config
        configured_agent = {}
        attempt_id = f"{self.config.worker_id}-{job.get('attempt') or 1}"
        checkout_dir = checkout_dir_for_job(self.config.work_dir, job_id)
        started = time.monotonic()
        duration_ms = 0
        job_error = ""
        resolved_commit = ""
        preflight: dict = {}
        graph_verified_report: dict = {}
        review_execution: dict = {}
        logs_summary = ""

        def scan_summary_review_execution() -> dict:
            return review_execution if isinstance(review_execution, dict) else {}

        try:
            job_config = worker_config_for_job(self.config, job)
            configured_agent = effective_agent_config_payload(job_config)
            graph_verified_review_enabled(job_config, job)
            checkout_dir = checkout_dir_for_job(job_config.work_dir, job_id)
            self.report_progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Cloning repository",
                config=job_config,
            )
            resolved_commit = clone_repository(job, checkout_dir)
            job["resolved_commit"] = resolved_commit
            job["commit"] = resolved_commit
            self.report_progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Repository cloned",
                config=job_config,
            )
            enforce_repository_limits(job_config, job, checkout_dir)
            self.report_progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository ready",
                config=job_config,
            )
            preflight = collect_preflight_metadata(job_config, job, checkout_dir)
            self.report_progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository preflight ready",
                config=job_config,
            )
            graph_summary = "GraphVerified will build full-repository review units during review."
            self.report_progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                graph_summary,
                config=job_config,
            )
            self.report_progress(
                job_id,
                "ai",
                PHASE_PROGRESS["ai"],
                "Running GraphVerified review",
                protocol_multiline_text(preflight.get("summary")),
                config=job_config,
            )

            def report_graph_verified_progress(event: object) -> None:
                self.report_progress(
                    job_id,
                    "ai",
                    PHASE_PROGRESS["ai"],
                    graph_verified_progress_message(event),
                    graph_verified_progress_logs_summary(event),
                    config=job_config,
                )

            graph_verified_report = run_graph_verified_review_payload(
                job_config,
                job,
                checkout_dir,
                progress_callback=report_graph_verified_progress,
            )
            projected_findings = graph_verified_summary_findings(graph_verified_report)
            summary = summarize(projected_findings)
            logs_summary = protocol_multiline_text(graph_verified_report.get("debugMarkdown"))[-1000:]
            effective_agent_config = configured_agent
            ai_usage = normalize_ai_usage({})
            self.report_progress(
                job_id,
                "ai",
                PHASE_PROGRESS["ai"],
                "GraphVerified review complete",
                logs_summary,
                config=job_config,
            )
            summary = summarize(projected_findings)
            duration_ms = int((time.monotonic() - started) * 1000)
            completion_error = graph_verified_completion_error(graph_verified_report)
            result_status = "failed" if completion_error else "done"
            if completion_error:
                logs_summary = completion_error[-1000:]
            log_graph_verified_completion(job_id, attempt_id, result_status, graph_verified_report, completion_error)
            result_summary = summary
            if result_status == "failed":
                result_summary = summarize([])
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
                payload["error"] = completion_error or "GraphVerified completion gate failed."
                payload["error_code"] = "GRAPH_VERIFIED_COMPLETION_FAILED"
                payload["errorCode"] = "GRAPH_VERIFIED_COMPLETION_FAILED"
            if ai_usage:
                payload["aiUsage"] = ai_usage
            payload["result_checksum"] = result_checksum(payload)
            self.report_progress(
                job_id,
                "report",
                100,
                "Uploading failed result" if result_status == "failed" else "Uploading result",
                logs_summary,
                config=job_config,
            )
            try:
                uploaded = self.upload_result_once_or_defer(job_id, payload)
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
            if not uploaded:
                write_scan_summary(
                    job_config,
                    job_id,
                    "upload_deferred",
                    duration_ms,
                    self.last_error or "result upload deferred for retry",
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
                "scope": "full-repository",
                "confirmedCount": 0,
                "rejectedCount": 0,
                "blockedCount": 1,
                "debugMarkdown": f"Graph-verified review failed before confirmation: {error}",
                "finalJson": {"confirmed": []},
            }
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
            log_graph_verified_completion(job_id, attempt_id, "failed", graph_verified_report, error)
            try:
                self.report_progress(
                    job_id,
                    "report",
                    PHASE_PROGRESS["report"],
                    "Uploading failed result",
                    error,
                    config=job_config,
                )
                uploaded = self.upload_result_once_or_defer(job_id, error_payload)
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
            if not uploaded:
                write_scan_summary(
                    job_config,
                    job_id,
                    "upload_deferred",
                    duration_ms,
                    self.last_error or "failed result upload deferred for retry",
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


def result_upload_dir(work_dir: Path) -> Path:
    return work_dir / _RESULT_UPLOAD_DIR_NAME


def result_upload_file(work_dir: Path, job_id: str) -> Path:
    return result_upload_dir(work_dir) / f"{safe_job_id(job_id)}.json"


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
    branch = str(job.get("branch") or "main")
    mirror_dir = repository_mirror_dir(checkout_dir.parent, job, clone_url)
    for attempt in range(2):
        try:
            ensure_repository_mirror(mirror_dir, clone_url, git_env)
            resolved_commit, mirror_ref = fetch_repository_ref(mirror_dir, branch=branch, commit=commit, env=git_env)
            clone_checkout_from_mirror(mirror_dir, checkout_dir, clone_url=clone_url, mirror_ref=mirror_ref)
            break
        except RuntimeError:
            remove_checkout_dir(checkout_dir)
            if attempt == 0:
                remove_checkout_dir(mirror_dir)
                continue
            raise
    return resolve_git_head(checkout_dir)


def repository_mirror_dir(work_dir: Path, job: dict, clone_url: str) -> Path:
    repo = str(job.get("repo") or "").strip()
    try:
        repo = validate_repo_full_name(repo)
        slug = repo.replace("/", "__").lower()
    except RuntimeError:
        slug = "repository"
    digest = hashlib.sha256(f"{repo}\n{clone_url}".encode("utf-8")).hexdigest()[:16]
    cache_root = (work_dir / _REPO_CACHE_DIR_NAME).resolve(strict=False)
    mirror_dir = (cache_root / f"{slug}-{digest}.git").resolve(strict=False)
    try:
        common = os.path.commonpath([str(cache_root), str(mirror_dir)])
    except ValueError as exc:
        raise RuntimeError("repository mirror cache path must stay inside checkout root") from exc
    if os.path.normcase(common) != os.path.normcase(str(cache_root)) or mirror_dir == cache_root:
        raise RuntimeError("repository mirror cache path must stay inside checkout root")
    return mirror_dir


def ensure_repository_mirror(mirror_dir: Path, clone_url: str, env: dict[str, str] | None) -> None:
    mirror_dir.parent.mkdir(parents=True, exist_ok=True)
    if not (mirror_dir / "HEAD").exists():
        run_git_command(["git", "init", "--bare", str(mirror_dir)], phase="mirror-init")
        run_git_command(["git", "-C", str(mirror_dir), "remote", "add", "origin", clone_url], phase="mirror-remote")
    else:
        run_git_command(["git", "-C", str(mirror_dir), "remote", "set-url", "origin", clone_url], phase="mirror-remote")


def fetch_repository_ref(mirror_dir: Path, *, branch: str, commit: str, env: dict[str, str] | None) -> tuple[str, str]:
    requested_commit = str(commit or "").strip()
    if requested_commit and requested_commit.lower() != "pending":
        target_ref = f"refs/pullwise/commits/{hashlib.sha256(requested_commit.encode('utf-8')).hexdigest()[:24]}"
        run_git_command(
            ["git", "-C", str(mirror_dir), "fetch", "--depth", "1", "origin", f"{requested_commit}:{target_ref}"],
            phase="fetch",
            env=env,
        )
        return resolve_git_ref(mirror_dir, target_ref), target_ref
    target_ref = f"refs/pullwise/branches/{hashlib.sha256(branch.encode('utf-8')).hexdigest()[:24]}"
    run_git_command(
        ["git", "-C", str(mirror_dir), "fetch", "--depth", "1", "origin", f"+refs/heads/{branch}:{target_ref}"],
        phase="fetch",
        env=env,
    )
    return resolve_git_ref(mirror_dir, target_ref), target_ref


def clone_checkout_from_mirror(mirror_dir: Path, checkout_dir: Path, *, clone_url: str, mirror_ref: str) -> None:
    checkout_ref = "refs/pullwise/checkout"
    run_git_command(
        ["git", "clone", "--shared", "--no-checkout", str(mirror_dir), str(checkout_dir)],
        phase="checkout-clone",
    )
    run_git_command(
        ["git", "-C", str(checkout_dir), "fetch", "--depth", "1", "origin", f"{mirror_ref}:{checkout_ref}"],
        phase="checkout-fetch",
    )
    run_git_command(
        ["git", "-C", str(checkout_dir), "checkout", "--detach", checkout_ref],
        phase="checkout",
    )
    run_git_command(
        ["git", "-C", str(checkout_dir), "remote", "set-url", "origin", clone_url],
        phase="checkout-remote",
    )


def resolve_git_ref(git_dir: Path, ref: str) -> str:
    commit = run_git_capture(["git", "-C", str(git_dir), "rev-parse", ref], phase="resolve-ref").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("git rev-parse did not return a commit SHA")
    return commit.lower()


def resolve_git_head(checkout_dir: Path) -> str:
    commit = run_git_capture(["git", "-C", str(checkout_dir), "rev-parse", "HEAD"], phase="resolve-head").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("git rev-parse HEAD did not return a commit SHA")
    return commit.lower()


def git_log_safe_url(value: object) -> str:
    text = str(value or "")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1].lower()
        path = parsed.path or ""
        return urllib.parse.urlunparse((parsed.scheme, host, path, "", "", ""))
    return text


def git_log_safe_arg(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:redacted@", text)
    text = re.sub(r"(https?://)([^/@\s]+):([^/@\s]+)@", r"\1redacted@", text)
    return re.sub(r"https?://[^\s'\"<>]+", lambda match: git_log_safe_url(match.group(0)), text)


def git_log_command(command: list[str]) -> str:
    return " ".join(shlex.quote(git_log_safe_arg(part)) for part in command)


def log_worker_git_event(phase: str, event: str, *, command: list[str] | None = None, detail: object = "") -> None:
    parts = [f"pullwise_worker git {event}", f"phase={phase}"]
    if command is not None:
        parts.append(f"command={git_log_command(command)}")
    if detail:
        parts.append(f"detail={git_log_safe_arg(detail)}")
    print(" ".join(parts), file=sys.stderr, flush=True)


def run_git_command(command: list[str], *, phase: str, env: dict[str, str] | None = None) -> None:
    run_git_capture(command, phase=phase, env=env)


def run_git_capture(command: list[str], *, phase: str, env: dict[str, str] | None = None) -> str:
    timeout_seconds = env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600)
    log_worker_git_event(phase, "start", command=command, detail=f"timeout={timeout_seconds}s")
    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            env=env,
        )
        log_worker_git_event(phase, "done", command=command)
        return completed.stdout or ""
    except subprocess.TimeoutExpired as exc:
        log_worker_git_event(phase, "timeout", command=command, detail=f"timeout={timeout_seconds}s")
        raise RuntimeError(f"git {phase} timed out after {timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        message = git_error_message(phase, exc)
        log_worker_git_event(phase, "failed", command=command, detail=message)
        raise RuntimeError(message) from exc


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
_VERIFICATION_STATUSES = {"verified", "static_proof", "potential_risk", "unverified"}
_REPO_CACHE_DIR_NAME = ".pullwise-repo-cache"
_RESULT_UPLOAD_DIR_NAME = ".pullwise-result-uploads"
_CHECKOUT_RUNTIME_DIR_NAMES.add(_REPO_CACHE_DIR_NAME)
_CHECKOUT_RUNTIME_DIR_NAMES.add(_RESULT_UPLOAD_DIR_NAME)
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
