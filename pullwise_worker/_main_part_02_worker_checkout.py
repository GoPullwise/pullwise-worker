from __future__ import annotations

# Imported by main.py and re-exported from the aggregate module.

from dataclasses import dataclass

from codereview.utils.process import ProcessCancelled, clear_process_cancel_event, set_process_cancel_event

from ._main_part_01_bootstrap import *  # noqa: F403

AGENT_REASONING_LEVELS = {"low", "medium", "high", "xhigh"}
AGENT_CONFIG_TEXT_MAX_LENGTH = 128
PROTOCOL_TEXT_MAX_LENGTH = 4000
PROTOCOL_SINGLE_LINE_TEXT_MAX_LENGTH = 500
GRAPH_VERIFIED_PROGRESS_UPLOAD_MIN_SECONDS = 2.0


@dataclass(frozen=True)
class WorkerReadinessSnapshot:
    checked_at: float = 0.0
    doctor_status: str = "not_ready"
    codex_ready: bool = False
    ready_providers: tuple[str, ...] = ()
    ready_for_claim: bool = False
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ready_providers", tuple(self.ready_providers or ()))

    def with_values(
        self,
        *,
        checked_at: float | None = None,
        doctor_status: str | None = None,
        codex_ready: bool | None = None,
        ready_providers: object = None,
        ready_for_claim: bool | None = None,
        last_error: str | None = None,
        keep_last_error: bool = True,
    ) -> "WorkerReadinessSnapshot":
        providers = (
            self.ready_providers
            if ready_providers is None
            else tuple(str(provider) for provider in ready_providers or [])
        )
        return WorkerReadinessSnapshot(
            checked_at=self.checked_at if checked_at is None else float(checked_at),
            doctor_status=self.doctor_status if doctor_status is None else str(doctor_status or "not_ready"),
            codex_ready=self.codex_ready if codex_ready is None else bool(codex_ready),
            ready_providers=providers,
            ready_for_claim=self.ready_for_claim if ready_for_claim is None else bool(ready_for_claim),
            last_error=self.last_error if keep_last_error and last_error is None else last_error,
        )

    def ready_provider_list(self) -> list[str]:
        return list(self.ready_providers)

    def has_codex_ready_evidence(self) -> bool:
        return self.doctor_status == "ok" or self.codex_ready or bool(self.ready_providers)

class WorkerJobCancelled(RuntimeError):
    pass


class PendingResultUploadRecordError(PullwiseRequestError):
    pass


class JobCancellationRegistry:
    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = Lock()

    def event(self, job_id: str) -> threading.Event:
        job_id = safe_job_id(job_id)
        with self._lock:
            event = self._events.get(job_id)
            if event is None:
                event = threading.Event()
                self._events[job_id] = event
            return event

    def cancel(self, job_ids: object) -> None:
        if not isinstance(job_ids, list):
            return
        with self._lock:
            for value in job_ids:
                try:
                    job_id = safe_job_id(value)
                except ValueError:
                    continue
                event = self._events.get(job_id)
                if event is None:
                    event = threading.Event()
                    self._events[job_id] = event
                event.set()

    def clear_if_matches(self, job_id: str, event: threading.Event) -> None:
        try:
            job_id = safe_job_id(job_id)
        except ValueError:
            return
        with self._lock:
            if self._events.get(job_id) is event:
                self._events.pop(job_id, None)

    def clear(self, job_id: str) -> None:
        try:
            job_id = safe_job_id(job_id)
        except ValueError:
            return
        with self._lock:
            self._events.pop(job_id, None)

    def is_cancelled(self, job_id: str) -> bool:
        try:
            job_id = safe_job_id(job_id)
        except ValueError:
            return False
        with self._lock:
            event = self._events.get(job_id)
        return bool(event is not None and event.is_set())


class WorkerJobSlot:
    def __init__(self) -> None:
        self.future: concurrent.futures.Future[None] | None = None
        self.job: dict | None = None

    def is_running(self) -> bool:
        return self.future is not None

    def start(self, executor: concurrent.futures.Executor, job_runner: object, job: dict) -> None:
        self.job = job
        self.future = executor.submit(job_runner, job)

    def wait(self, *, timeout: float | None = None) -> None:
        if self.future is None:
            return
        wait_kwargs = {}
        if timeout is not None:
            wait_kwargs["timeout"] = timeout
            wait_kwargs["return_when"] = concurrent.futures.FIRST_COMPLETED
        concurrent.futures.wait([self.future], **wait_kwargs)

    def collect_finished(self) -> tuple[dict, Exception | None] | None:
        if self.future is None or not self.future.done():
            return None
        future = self.future
        job = self.job or {}
        self.future = None
        self.job = None
        try:
            future.result()
        except Exception as exc:
            return job, exc
        return job, None

    def active_job_ids(self, pending_job_ids: list[str]) -> list[str]:
        active_job_ids = list(pending_job_ids)
        if self.job is None:
            return active_job_ids
        try:
            active_job_id = safe_job_id(self.job.get("job_id"))
        except ValueError:
            return active_job_ids
        if active_job_id not in active_job_ids:
            active_job_ids.append(active_job_id)
        return active_job_ids


class ResultUploadManager:
    def __init__(self, worker: object) -> None:
        self.worker = worker
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pullwise-result-upload",
        )
        self.pending_uploads: dict[str, tuple[concurrent.futures.Future[None], Path]] = {}
        self.lock = Lock()

    @property
    def config(self) -> object:
        return self.worker.config

    @property
    def client(self) -> object:
        return self.worker.client

    def _config_int(self, name: str, default: int, *, minimum: int = 0) -> int:
        try:
            value = int(getattr(self.config, name, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        return max(minimum, value)

    def pending_backoff_base_seconds(self) -> int:
        return self._config_int("result_upload_pending_backoff_base_seconds", 30, minimum=1)

    def pending_backoff_max_seconds(self) -> int:
        return self._config_int("result_upload_pending_backoff_max_seconds", 15 * 60, minimum=1)

    def pending_max_age_seconds(self) -> int:
        return self._config_int("result_upload_pending_max_age_seconds", 7 * 24 * 60 * 60, minimum=60)

    def pending_max_attempts(self) -> int:
        return self._config_int("result_upload_pending_max_attempts", 100, minimum=1)

    def upload_with_retry(self, job_id: str, payload: dict) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.result_upload_attempts + 1):
            if self.worker.job_cancel_requested(job_id):
                raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
            try:
                self.client.result(job_id, payload)
                return
            except PullwiseHTTPError as exc:
                if exc.status_code == 409:
                    raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates") from exc
                if exc.status_code < 500 or attempt >= self.config.result_upload_attempts:
                    raise
                last_error = exc
            except PullwiseRequestError as exc:
                last_error = exc
                if attempt >= self.config.result_upload_attempts:
                    raise
            if self.worker.job_cancel_requested(job_id):
                raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
            if attempt < self.config.result_upload_attempts:
                self.wait_before_retry(job_id, min(30, 2 ** (attempt - 1)))
        if last_error:
            raise last_error

    def wait_before_retry(self, job_id: str, delay_seconds: float) -> None:
        event = self.worker.job_cancel_event(job_id)
        if event.wait(max(0.0, float(delay_seconds or 0))):
            raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")

    def load_pending(self) -> None:
        pending_dir = result_upload_dir(self.config.work_dir)
        if not pending_dir.exists():
            return
        if pending_dir.is_symlink():
            self.worker.last_error = f"pending result upload directory must not be a symlink: {pending_dir.name}"
            return
        now = int(time.time())
        for path in sorted(pending_dir.glob("*.json")):
            try:
                record = self.read_record_data(path)
                job_id = safe_job_id(record.get("job_id"))
            except PendingResultUploadRecordError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.worker.last_error = (
                    f"pending result upload record permanently invalid for {path.name}: "
                    f"{redact_secrets(str(exc), self.config)}"
                )[:500]
                continue
            reason = self.pending_dead_letter_reason(record, path, now)
            if reason:
                self.dead_letter(path, job_id, record, reason)
                self.worker.last_error = f"result upload dead-lettered for {job_id}: {reason}"[:500]
                continue
            self.worker.schedule_pending_result_upload(job_id, path)

    def pending_job_ids(self) -> list[str]:
        with self.lock:
            return list(self.pending_uploads.keys())

    def has_pending(self, job_id: str) -> bool:
        try:
            job_id = safe_job_id(job_id)
        except ValueError:
            return False
        with self.lock:
            return job_id in self.pending_uploads

    def collect(self) -> None:
        done_uploads: list[tuple[str, concurrent.futures.Future[None], Path]] = []
        with self.lock:
            for job_id, (future, path) in list(self.pending_uploads.items()):
                if future.done():
                    done_uploads.append((job_id, future, path))
                    self.pending_uploads.pop(job_id, None)
        for job_id, future, path in done_uploads:
            try:
                future.result()
                self.worker.clear_job_cancel_event_by_id(job_id)
                self.clear_error(job_id)
            except PendingResultUploadRecordError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.worker.clear_job_cancel_event_by_id(job_id)
                self.worker.last_error = (
                    f"pending result upload record permanently invalid for {job_id}: "
                    f"{redact_secrets(str(exc), self.config)}"
                )[:500]
            except WorkerJobCancelled as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.worker.clear_job_cancel_event_by_id(job_id)
                self.worker.last_error = redact_secrets(str(exc), self.config)[:500]
            except PullwiseHTTPError as exc:
                if exc.status_code < 500:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.worker.clear_job_cancel_event_by_id(job_id)
                    self.worker.last_error = (
                        f"result upload permanently failed for {job_id}: "
                        f"{redact_secrets(str(exc), self.config)}"
                    )[:500]
                    continue
                self.defer_failed_upload(job_id, path, exc)
            except PullwiseRequestError as exc:
                self.defer_failed_upload(job_id, path, exc)
            except Exception as exc:
                self.defer_failed_upload(job_id, path, exc)

    def clear_error(self, job_id: str) -> None:
        if not self.worker.last_error:
            return
        prefixes = (
            f"result upload retry failed for {job_id}:",
            f"result upload retry deferred for {job_id}",
            f"result upload failed for {job_id}:",
            f"result upload deferred for {job_id}:",
            f"result upload dead-lettered for {job_id}:",
        )
        if any(self.worker.last_error.startswith(prefix) for prefix in prefixes):
            self.worker.last_error = None

    def schedule(self, job_id: str, path: Path) -> None:
        job_id = safe_job_id(job_id)
        with self.lock:
            current = self.pending_uploads.get(job_id)
            if current and not current[0].done():
                return
            future = self.executor.submit(self.worker.upload_pending_result_file, path)
            self.pending_uploads[job_id] = (future, path)

    def upload_file(self, path: Path) -> None:
        record = self.read_record_data(path)
        job_id = safe_job_id(record.get("job_id"))
        delay_seconds = self.pending_retry_delay_seconds(record)
        if delay_seconds > 0:
            self.wait_before_retry(job_id, min(delay_seconds, self.pending_backoff_max_seconds()))
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise PendingResultUploadRecordError("pending result upload payload must be an object")
        self.worker.upload_result_with_retry(job_id, payload)
        path.unlink(missing_ok=True)

    def record(self, path: Path) -> tuple[str, dict]:
        record = self.read_record_data(path)
        return safe_job_id(record.get("job_id")), record["payload"]

    def read_record_data(self, path: Path) -> dict:
        if path.parent.is_symlink():
            raise PendingResultUploadRecordError("pending result upload directory must not be a symlink")
        if path.is_symlink():
            raise PendingResultUploadRecordError("pending result upload record must not be a symlink")
        try:
            record = json.loads(read_no_follow_text_file(path, max_bytes=_PENDING_RESULT_UPLOAD_RECORD_MAX_BYTES))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PendingResultUploadRecordError(f"pending result upload record is unreadable: {exc}") from exc
        if not isinstance(record, dict):
            raise PendingResultUploadRecordError("pending result upload record must be an object")
        try:
            job_id = safe_job_id(record.get("job_id"))
        except ValueError as exc:
            raise PendingResultUploadRecordError(f"pending result upload job_id is invalid: {exc}") from exc
        expected_path = result_upload_file(self.config.work_dir, job_id)
        if path.resolve(strict=False) != expected_path.resolve(strict=False):
            raise PendingResultUploadRecordError("pending result upload filename does not match job_id")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise PendingResultUploadRecordError("pending result upload payload must be an object")
        normalized = dict(record)
        normalized["job_id"] = job_id
        normalized["payload"] = payload
        return normalized

    def write_record(self, path: Path, record: dict) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > _PENDING_RESULT_UPLOAD_RECORD_MAX_BYTES:
            raise RuntimeError("pending result upload record is too large")
        write_no_follow_text_file(path, payload)

    def defer(self, job_id: str, payload: dict) -> Path:
        pending_dir = result_upload_dir(self.config.work_dir)
        pending_dir.mkdir(parents=True, exist_ok=True)
        path = result_upload_file(self.config.work_dir, job_id)
        now = int(time.time())
        self.write_record(
            path,
            {
                "job_id": safe_job_id(job_id),
                "created_at": now,
                "attempts": 0,
                "next_attempt_at": 0,
                "payload": payload,
            },
        )
        self.worker.schedule_pending_result_upload(job_id, path)
        return path

    def upload_once_or_defer(self, job_id: str, payload: dict) -> bool:
        if self.worker.job_cancel_requested(job_id):
            raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
        try:
            self.client.result(job_id, payload)
            return True
        except PullwiseHTTPError as exc:
            if exc.status_code < 500:
                raise
            error = exc
        except PullwiseRequestError as exc:
            error = exc
        if self.worker.job_cancel_requested(job_id):
            raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
        self.worker.defer_result_upload(job_id, payload)
        self.worker.last_error = f"result upload deferred for {job_id}: {redact_secrets(str(error), self.config)}"[:500]
        return False

    def pending_record_int(self, record: dict, key: str, default: int) -> int:
        try:
            return int(record.get(key, default))
        except (TypeError, ValueError, OverflowError):
            return default

    def pending_record_created_at(self, record: dict, path: Path, now: int) -> int:
        created_at = self.pending_record_int(record, "created_at", 0)
        if created_at > 0:
            return created_at
        try:
            stat_result = path.lstat()
            return int(stat_result.st_mtime)
        except OSError:
            return now

    def pending_retry_delay_seconds(self, record: dict) -> int:
        next_attempt_at = self.pending_record_int(record, "next_attempt_at", 0)
        if next_attempt_at <= 0:
            return 0
        return max(0, next_attempt_at - int(time.time()))

    def pending_backoff_seconds(self, attempts: int) -> int:
        exponent = min(max(0, attempts - 1), 10)
        return min(self.pending_backoff_max_seconds(), self.pending_backoff_base_seconds() * (2 ** exponent))

    def pending_dead_letter_reason(self, record: dict, path: Path, now: int) -> str:
        attempts = self.pending_record_int(record, "attempts", 0)
        if attempts >= self.pending_max_attempts():
            return f"exceeded {self.pending_max_attempts()} retry attempts"
        created_at = self.pending_record_created_at(record, path, now)
        max_age = self.pending_max_age_seconds()
        if max_age > 0 and now - created_at >= max_age:
            return f"exceeded {max_age} seconds pending retention"
        return ""

    def defer_failed_upload(self, job_id: str, path: Path, exc: Exception) -> None:
        now = int(time.time())
        error_text = redact_secrets(str(exc), self.config)
        try:
            record = self.read_record_data(path)
        except PendingResultUploadRecordError as record_exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self.worker.clear_job_cancel_event_by_id(job_id)
            self.worker.last_error = (
                f"pending result upload record permanently invalid for {job_id}: "
                f"{redact_secrets(str(record_exc), self.config)}"
            )[:500]
            return
        record["created_at"] = self.pending_record_created_at(record, path, now)
        record["attempts"] = self.pending_record_int(record, "attempts", 0) + 1
        record["last_attempt_at"] = now
        record["last_error"] = error_text[:500]
        reason = self.pending_dead_letter_reason(record, path, now)
        if reason:
            self.dead_letter(path, safe_job_id(job_id), record, reason)
            self.worker.clear_job_cancel_event_by_id(job_id)
            self.worker.last_error = f"result upload dead-lettered for {job_id}: {reason}; {error_text}"[:500]
            return
        delay_seconds = self.pending_backoff_seconds(self.pending_record_int(record, "attempts", 1))
        record["next_attempt_at"] = now + delay_seconds
        self.write_record(path, record)
        self.worker.last_error = (
            f"result upload retry deferred for {job_id} until {record['next_attempt_at']}: {error_text}"
        )[:500]
        self.worker.schedule_pending_result_upload(job_id, path)

    def dead_letter(self, path: Path, job_id: str, record: dict, reason: str) -> Path:
        pending_dir = result_upload_dir(self.config.work_dir)
        dead_dir = result_upload_dead_letter_dir(self.config.work_dir)
        if pending_dir.is_symlink() or dead_dir.is_symlink():
            raise RuntimeError("pending result upload dead-letter directory must not be a symlink")
        dead_dir.mkdir(parents=True, exist_ok=True)
        dead_path = result_upload_dead_letter_file(self.config.work_dir, job_id)
        dead_record = dict(record)
        dead_record["dead_letter_at"] = int(time.time())
        dead_record["dead_letter_reason"] = reason
        self.write_record(dead_path, dead_record)
        path.unlink(missing_ok=True)
        return dead_path
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


def bounded_positive_int(value: object, *, default: int, maximum: int, minimum: int = 1) -> int:
    if isinstance(value, bool):
        parsed = int(default or 0)
    else:
        try:
            parsed = int(value if value is not None else default)
        except (OverflowError, TypeError, ValueError):
            parsed = int(default or 0)
    return max(minimum, min(maximum, parsed))


def normalize_job_attempt(value: object) -> int:
    if value is None or value == "":
        return 1
    if isinstance(value, bool):
        raise ValueError("Worker job attempt must be a positive integer.")
    try:
        attempt = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("Worker job attempt must be a positive integer.") from exc
    if attempt < 1 or attempt > 1_000_000:
        raise ValueError("Worker job attempt must be between 1 and 1000000.")
    return attempt


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


def graph_verified_completion_error(report: dict | None, projected_findings: list[dict] | None = None) -> str:
    if not isinstance(report, dict) or not report:
        return "GraphVerified review did not produce a report."
    visible_findings = len(projected_findings) if isinstance(projected_findings, list) else 0
    blocked_count = graph_verified_report_int(report.get("blockedCount"))
    if blocked_count and not report.get("runId") and visible_findings == 0:
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
    if confirmed > 0 and visible_findings == 0:
        return "GraphVerified confirmed findings, but none were safe to show in the worker result payload."

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
    config.max_repo_files = bounded_positive_int(
        max_repo_files,
        default=_DEFAULT_MAX_REPO_FILES,
        maximum=_MAX_REPO_LIMIT_FILES,
    )
    config.max_repo_bytes = bounded_positive_int(
        max_repo_bytes,
        default=_DEFAULT_MAX_REPO_BYTES,
        maximum=_MAX_REPO_LIMIT_BYTES,
    )
    return config


class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.client = PullwiseClient(config)
        self.last_error: str | None = None
        self._readiness_snapshot = WorkerReadinessSnapshot()
        self._empty_poll_count = 0
        self._error_poll_count = 0
        self._last_cleanup_at = 0.0
        self._machine_metrics_checked_at = 0.0
        self._cleanup_future: concurrent.futures.Future[None] | None = None
        self._cleanup_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pullwise-cleanup",
        )
        self.result_uploads = ResultUploadManager(self)
        self._result_upload_executor = self.result_uploads.executor
        self._pending_result_uploads = self.result_uploads.pending_uploads
        self._pending_result_uploads_lock = self.result_uploads.lock
        self._job_cancellations = JobCancellationRegistry()
        self._graph_verified_progress_upload_at = 0.0
        self._graph_verified_progress_upload_stage = ""
        self.log_tailers: dict[str, WorkerLogStreamTailer] = {}

    def _set_readiness_snapshot(self, snapshot: WorkerReadinessSnapshot) -> None:
        self._readiness_snapshot = snapshot

    def _update_readiness_snapshot(self, **values: object) -> None:
        snapshot = self._readiness_snapshot
        self._readiness_snapshot = snapshot.with_values(
            checked_at=values.get("checked_at") if "checked_at" in values else None,
            doctor_status=values.get("doctor_status") if "doctor_status" in values else None,
            codex_ready=values.get("codex_ready") if "codex_ready" in values else None,
            ready_providers=values.get("ready_providers") if "ready_providers" in values else None,
            ready_for_claim=values.get("ready_for_claim") if "ready_for_claim" in values else None,
            last_error=values.get("last_error") if "last_error" in values else None,
            keep_last_error="last_error" not in values,
        )

    @property
    def _readiness_checked_at(self) -> float:
        return self._readiness_snapshot.checked_at

    @_readiness_checked_at.setter
    def _readiness_checked_at(self, value: object) -> None:
        self._update_readiness_snapshot(checked_at=float(value or 0.0))

    @property
    def _doctor_status(self) -> str:
        return self._readiness_snapshot.doctor_status

    @_doctor_status.setter
    def _doctor_status(self, value: object) -> None:
        status = str(value or "not_ready")
        self._update_readiness_snapshot(doctor_status=status, ready_for_claim=status == "ok")

    @property
    def _codex_ready(self) -> bool:
        return self._readiness_snapshot.codex_ready

    @_codex_ready.setter
    def _codex_ready(self, value: object) -> None:
        self._update_readiness_snapshot(codex_ready=bool(value))

    @property
    def _ready_providers(self) -> list[str]:
        return self._readiness_snapshot.ready_provider_list()

    @_ready_providers.setter
    def _ready_providers(self, value: object) -> None:
        self._update_readiness_snapshot(ready_providers=value or [])

    def handle_log_session(self, session: object) -> None:
        session_id = log_stream_session_id(session if isinstance(session, dict) else None)
        if not session_id:
            self.log_tailers.clear()
            return
        if session_id not in self.log_tailers:
            self.log_tailers = {session_id: WorkerLogStreamTailer(self.config, session)}
        tailer = self.log_tailers[session_id]
        try:
            entries, state = tailer.collect()
        except Exception as exc:
            self.last_error = f"log stream collection failed: {redact_secrets(str(exc), self.config)}"[:500]
            return
        if not entries:
            return
        try:
            upload_log_stream_entries(self.client, session_id, entries)
        except PullwiseRequestError as exc:
            self.last_error = f"log stream upload failed: {redact_secrets(str(exc), self.config)}"[:500]
            return
        try:
            tailer.commit(state)
        except Exception as exc:
            self.last_error = f"log stream checkpoint failed: {redact_secrets(str(exc), self.config)}"[:500]

    def job_cancel_event(self, job_id: str) -> threading.Event:
        return self._job_cancellations.event(job_id)

    def cancel_server_jobs(self, job_ids: object) -> None:
        self._job_cancellations.cancel(job_ids)

    def clear_job_cancel_event(self, job_id: str, event: threading.Event) -> None:
        if self.has_pending_result_upload(job_id):
            return
        self._job_cancellations.clear_if_matches(job_id, event)

    def clear_job_cancel_event_by_id(self, job_id: str) -> None:
        self._job_cancellations.clear(job_id)

    def job_cancel_requested(self, job_id: str) -> bool:
        return self._job_cancellations.is_cancelled(job_id)

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.load_pending_result_uploads()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            job_slot = WorkerJobSlot()

            def collect_finished_job() -> None:
                finished = job_slot.collect_finished()
                if finished is None:
                    return
                job, exc = finished
                if exc is not None:
                    self.last_error = f"job {job.get('job_id')} failed unexpectedly: {exc}"[:500]

            def collect_once_background_work(active_job_ids: list[str], *, claimed_jobs: int, loop_error: bool) -> None:
                self.collect_result_uploads()
                if not job_slot.is_running() and not claimed_jobs and not loop_error:
                    self.schedule_cleanup_resources_if_due(active_job_ids)
                    if self._cleanup_future is not None:
                        concurrent.futures.wait([self._cleanup_future])
                        self.collect_cleanup()

            while True:
                collect_finished_job()
                self.collect_result_uploads()
                self.collect_cleanup()
                job_running = job_slot.is_running()
                ready = not job_running
                loop_error = False
                claimed_jobs = 0
                heartbeat_payload: dict = {}
                readiness_snapshot = self._readiness_snapshot
                machine_metrics = self.machine_metrics_if_due()
                active_job_ids = job_slot.active_job_ids(self.pending_result_job_ids())
                try:
                    heartbeat_response = self.client.heartbeat(
                        running_jobs=1 if job_running else 0,
                        active_job_ids=active_job_ids,
                        last_error=self.last_error,
                        doctor_status=readiness_snapshot.doctor_status,
                        codex_ready=readiness_snapshot.codex_ready,
                        ready_providers=readiness_snapshot.ready_provider_list(),
                        doctor_checked_at=int(readiness_snapshot.checked_at) if readiness_snapshot.checked_at else None,
                        machine_metrics=machine_metrics,
                    )
                    if isinstance(heartbeat_response, dict):
                        heartbeat_payload = heartbeat_response
                except PullwiseRequestError as exc:
                    self.last_error = f"heartbeat failed: {redact_secrets(str(exc), self.config)}"[:500]
                    loop_error = True
                self.cancel_server_jobs(
                    heartbeat_payload.get("cancelled_job_ids")
                    if "cancelled_job_ids" in heartbeat_payload
                    else heartbeat_payload.get("cancelledJobIds")
                )
                worker_state = heartbeat_payload.get("worker") if isinstance(heartbeat_payload.get("worker"), dict) else {}
                command = heartbeat_payload.get("command") if isinstance(heartbeat_payload.get("command"), dict) else None
                if not getattr(self.config, "lifecycle_watcher_enabled", False):
                    self.handle_log_session(heartbeat_payload.get("logSession") or heartbeat_payload.get("log_session"))
                if worker_state.get("status") == "disabled":
                    ready = False
                if lifecycle_command_parts(command):
                    ready = False
                    if not job_running and not loop_error and self.handle_lifecycle_command(command):
                        return
                if self.cleanup_is_running():
                    ready = False
                if not job_running and not loop_error and ready:
                    ready = self.refresh_readiness_if_due()
                if ready and not job_running:
                    job = None
                    if not loop_error:
                        try:
                            job = self.client.claim()
                            if job:
                                validate_claimed_job(job)
                        except PullwiseRequestError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
                        except ValueError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
                            job = None
                    if job:
                        job_slot.start(executor, self.run_job, job)
                    claimed_jobs = 1 if job else 0
                    if once:
                        job_slot.wait()
                        collect_finished_job()
                        collect_once_background_work(active_job_ids, claimed_jobs=claimed_jobs, loop_error=loop_error)
                        return
                elif once:
                    job_slot.wait()
                    collect_finished_job()
                    collect_once_background_work(active_job_ids, claimed_jobs=claimed_jobs, loop_error=loop_error)
                    return
                if not job_slot.is_running() and not claimed_jobs and not loop_error:
                    self.schedule_cleanup_resources_if_due(active_job_ids)
                sleep_seconds = self.next_poll_sleep(
                    claimed_jobs=claimed_jobs,
                    loop_error=loop_error,
                    worker_busy=job_slot.is_running(),
                )
                if job_slot.is_running():
                    job_slot.wait(timeout=sleep_seconds)
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
        parsed = lifecycle_command_parts(command)
        if parsed is None:
            return False
        command_id, action = parsed
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
        snapshot = self._readiness_snapshot
        if current - snapshot.checked_at < self.config.readiness_check_seconds:
            return snapshot.ready_for_claim
        try:
            checks, _provider_ready, ready_providers = worker_readiness_state(self.config)
        except Exception as exc:
            last_error = f"worker not ready: readiness check failed: {redact_secrets(str(exc), self.config)}"[:500]
            self._set_readiness_snapshot(
                WorkerReadinessSnapshot(
                    checked_at=current,
                    doctor_status="degraded",
                    codex_ready=False,
                    ready_providers=(),
                    ready_for_claim=False,
                    last_error=last_error,
                )
            )
            self.last_error = last_error
            return False
        failed_check = first_failed_check(checks)
        if (
            failed_check is not None
            and failed_check[0] == "codex_ready"
            and "deferred" in str(failed_check[2] or "").lower()
            and snapshot.has_codex_ready_evidence()
        ):
            self._set_readiness_snapshot(snapshot.with_values(checked_at=current))
            return True
        codex_ready = readiness_check_ok(checks, "codex_ready")
        last_error = None if failed_check is None else readiness_error_message(failed_check, self.config)
        self._set_readiness_snapshot(
            WorkerReadinessSnapshot(
                checked_at=current,
                doctor_status="degraded" if failed_check else "ok",
                codex_ready=codex_ready,
                ready_providers=tuple(ready_providers),
                ready_for_claim=failed_check is None,
                last_error=last_error,
            )
        )
        self.last_error = last_error
        return failed_check is None

    def upload_result_with_retry(self, job_id: str, payload: dict) -> None:
        self.result_uploads.upload_with_retry(job_id, payload)

    def wait_before_result_upload_retry(self, job_id: str, delay_seconds: float) -> None:
        self.result_uploads.wait_before_retry(job_id, delay_seconds)

    def load_pending_result_uploads(self) -> None:
        self.result_uploads.load_pending()

    def pending_result_job_ids(self) -> list[str]:
        return self.result_uploads.pending_job_ids()

    def has_pending_result_upload(self, job_id: str) -> bool:
        return self.result_uploads.has_pending(job_id)

    def collect_result_uploads(self) -> None:
        self.result_uploads.collect()

    def clear_result_upload_error(self, job_id: str) -> None:
        self.result_uploads.clear_error(job_id)

    def schedule_pending_result_upload(self, job_id: str, path: Path) -> None:
        self.result_uploads.schedule(job_id, path)

    def upload_pending_result_file(self, path: Path) -> None:
        self.result_uploads.upload_file(path)

    def pending_result_upload_record(self, path: Path) -> tuple[str, dict]:
        return self.result_uploads.record(path)

    def write_pending_result_upload_record(self, path: Path, record: dict) -> None:
        self.result_uploads.write_record(path, record)

    def defer_result_upload(self, job_id: str, payload: dict) -> Path:
        return self.result_uploads.defer(job_id, payload)

    def upload_result_once_or_defer(self, job_id: str, payload: dict) -> bool:
        return self.result_uploads.upload_once_or_defer(job_id, payload)
    def graph_verified_progress_upload_due(self, event: object) -> bool:
        if not isinstance(event, dict):
            return True
        current = event.get("current")
        total = event.get("total")
        stage = clean_protocol_text(event.get("stage"), 80)
        if not isinstance(current, int) or not isinstance(total, int) or total <= 0:
            return True
        if current <= 0 or current >= total:
            return True
        now = time.monotonic()
        if stage != self._graph_verified_progress_upload_stage:
            self._graph_verified_progress_upload_stage = stage
            self._graph_verified_progress_upload_at = now
            return True
        if now - self._graph_verified_progress_upload_at >= GRAPH_VERIFIED_PROGRESS_UPLOAD_MIN_SECONDS:
            self._graph_verified_progress_upload_at = now
            return True
        return False

    def record_local_progress(
        self,
        config: WorkerConfig,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
    ) -> None:
        if self.job_cancel_requested(job_id):
            raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
        try:
            write_scan_progress_summary(
                config,
                job_id,
                phase,
                progress,
                message,
                logs_summary,
                log_time=int(time.time()),
            )
        except Exception:
            pass

    def report_progress(
        self,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
        *,
        config: WorkerConfig | None = None,
        write_local: bool = True,
    ) -> bool:
        active_config = config or self.config
        if self.job_cancel_requested(job_id):
            raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates")
        log_time = int(time.time())
        if write_local:
            try:
                write_scan_progress_summary(
                    active_config,
                    job_id,
                    phase,
                    progress,
                    message,
                    logs_summary,
                    log_time=log_time,
                )
            except Exception:
                pass
        try:
            self.client.progress(job_id, phase, progress, message, logs_summary, log_time=log_time)
            return True
        except PullwiseHTTPError as exc:
            if exc.status_code == 409:
                raise WorkerJobCancelled(f"job {job_id} is no longer accepting worker updates") from exc
            self.last_error = (
                f"progress update failed for {job_id} at {phase}: "
                f"{redact_secrets(str(exc), active_config)}"
            )[:500]
            return False
        except Exception as exc:
            self.last_error = (
                f"progress update failed for {job_id} at {phase}: "
                f"{redact_secrets(str(exc), active_config)}"
            )[:500]
            return False

    def record_server_cancelled_job(
        self,
        config: WorkerConfig,
        job_id: str,
        duration_ms: int,
        review_execution: dict | None = None,
        message: str = "",
    ) -> None:
        self.last_error = redact_secrets(
            message or f"job {job_id} is no longer accepting worker updates",
            config,
        )[:500]
        write_scan_summary(config, job_id, "cancelled", duration_ms, self.last_error, review_execution)

    def run_job(self, job: dict) -> None:
        job_id = safe_job_id(job.get("job_id"))
        job_config = self.config
        configured_agent = {}
        attempt_id = f"{self.config.worker_id}-{normalize_job_attempt(job.get('attempt'))}"
        checkout_dir = checkout_dir_for_job(self.config.work_dir, job_id)
        started = time.monotonic()
        duration_ms = 0
        job_error = ""
        resolved_commit = ""
        preflight: dict = {}
        graph_verified_report: dict = {}
        review_execution: dict = {}
        logs_summary = ""
        cancel_event = self.job_cancel_event(job_id)
        set_process_cancel_event(cancel_event)

        def scan_summary_review_execution() -> dict:
            return review_execution if isinstance(review_execution, dict) else {}

        try:
            job_config = worker_config_for_job(self.config, job)
            configured_agent = effective_agent_config_payload(job_config)
            checkout_dir = checkout_dir_for_job(job_config.work_dir, job_id)
            self.report_progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Cloning repository",
                config=job_config,
            )
            resolved_commit = clone_repository(job, checkout_dir, limits_config=job_config)
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
                graph_verified_progress_percent({"stage": "setup"}),
                "Running GraphVerified review",
                protocol_multiline_text(preflight.get("summary")),
                config=job_config,
            )

            def report_graph_verified_progress(event: object) -> None:
                message = graph_verified_progress_message(event)
                event_logs_summary = graph_verified_progress_logs_summary(event)
                event_progress = graph_verified_progress_percent(event)
                self.record_local_progress(
                    job_config,
                    job_id,
                    "ai",
                    event_progress,
                    message,
                    event_logs_summary,
                )
                if not self.graph_verified_progress_upload_due(event):
                    return
                self.report_progress(
                    job_id,
                    "ai",
                    event_progress,
                    message,
                    event_logs_summary,
                    config=job_config,
                    write_local=False,
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
            self.report_progress(
                job_id,
                "ai",
                graph_verified_progress_percent({"stage": "report", "current": 1, "total": 1}),
                "GraphVerified review complete",
                logs_summary,
                config=job_config,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            completion_error = graph_verified_completion_error(graph_verified_report, projected_findings)
            completion_error_code = "GRAPH_VERIFIED_COMPLETION_FAILED"
            if completion_error and codex_readiness_failure_cacheable(completion_error):
                readiness_kind = codex_readiness_issue_kind(completion_error)
                completion_error_code = {
                    "codex_auth_required": "CODEX_AUTH_REQUIRED",
                    "codex_auth_expired": "CODEX_AUTH_EXPIRED",
                    "codex_authorization_failed": "CODEX_AUTHORIZATION_FAILED",
                    "codex_subscription_inactive": "CODEX_SUBSCRIPTION_INACTIVE",
                    "codex_quota_exhausted": "CODEX_QUOTA_EXHAUSTED",
                    "codex_version_unsupported": "CODEX_VERSION_UNSUPPORTED",
                }.get(readiness_kind, completion_error_code)
                mark_codex_auth_failure(job_config, completion_error)
                auth_detail = codex_readiness_issue_detail(completion_error, job_config) or completion_error
                self._set_readiness_snapshot(
                    WorkerReadinessSnapshot(
                        checked_at=time.time(),
                        doctor_status="degraded",
                        codex_ready=False,
                        ready_providers=(),
                        ready_for_claim=False,
                        last_error=auth_detail[:500],
                    )
                )
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
                payload["error_code"] = completion_error_code
                payload["errorCode"] = completion_error_code
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
            except PullwiseHTTPError as exc:
                if exc.status_code == 409:
                    self.record_server_cancelled_job(
                        job_config,
                        job_id,
                        duration_ms,
                        scan_summary_review_execution(),
                    )
                    return
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
        except WorkerJobCancelled as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.record_server_cancelled_job(
                job_config,
                job_id,
                duration_ms,
                scan_summary_review_execution(),
                str(exc),
            )
            return
        except ProcessCancelled as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.record_server_cancelled_job(
                job_config,
                job_id,
                duration_ms,
                scan_summary_review_execution(),
                str(exc),
            )
            return
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
            except WorkerJobCancelled as cancel_exc:
                self.record_server_cancelled_job(
                    job_config,
                    job_id,
                    duration_ms,
                    scan_summary_review_execution(),
                    str(cancel_exc),
                )
                return
            except PullwiseHTTPError as upload_exc:
                if upload_exc.status_code == 409:
                    self.record_server_cancelled_job(
                        job_config,
                        job_id,
                        duration_ms,
                        scan_summary_review_execution(),
                    )
                    return
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
            clear_process_cancel_event()
            self.clear_job_cancel_event(job_id, cancel_event)
            if job_error and job_config.failed_checkout_retention_seconds > 0:
                marker = failed_checkout_marker(checkout_dir)
                write_failed_checkout_marker(marker, int(time.time()) + job_config.failed_checkout_retention_seconds)
            else:
                try:
                    cleanup_job_checkout(checkout_dir)
                except Exception as cleanup_exc:
                    cleanup_error = f"checkout cleanup failed for {job_id}: {redact_secrets(str(cleanup_exc), job_config)}"
                    self.last_error = f"{self.last_error}; {cleanup_error}"[:500] if self.last_error else cleanup_error[:500]


def safe_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not job_id or len(job_id) > _MAX_JOB_ID_LENGTH or job_id in {".", ".."} or not _SAFE_JOB_ID_RE.match(job_id):
        raise ValueError("job_id contains unsafe path characters")
    return job_id


def validate_claimed_job(job: object) -> dict:
    if not isinstance(job, dict):
        raise ValueError("claim response job must be an object")
    try:
        safe_job_id(job.get("job_id"))
        if "attempt" in job:
            job["attempt"] = normalize_job_attempt(job.get("attempt"))
        if "commit" in job:
            normalize_git_commit_or_pending(job.get("commit"))
        if "branch" in job:
            normalize_git_branch(job.get("branch") or "main")
        repo = str(job.get("repo") or "").strip()
        clone_url = job.get("clone_url")
        clone_token = job.get("clone_token")
        if repo or clone_url or clone_token_value(clone_token):
            effective_clone_url = str(clone_url or "")
            if not effective_clone_url and repo:
                effective_clone_url = f"{worker_github_web_url()}/{repo}.git"
            trusted_or_local_clone_url(job, effective_clone_url, clone_token)
            if clone_token_value(clone_token):
                trusted_clone_url_for_token(job, effective_clone_url, clone_token)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    return job


def lifecycle_command_parts(command: object) -> tuple[str, str] | None:
    if not isinstance(command, dict):
        return None
    command_id = str(command.get("id") or "").strip()
    if not command_id or len(command_id) > 128 or any(char in command_id for char in "\r\n\x00"):
        return None
    action = str(command.get("command") or "").strip().lower()
    if action not in {"stop", "uninstall"}:
        return None
    return command_id, action


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


def result_upload_dead_letter_dir(work_dir: Path) -> Path:
    return result_upload_dir(work_dir) / _RESULT_UPLOAD_DEAD_LETTER_DIR_NAME


def result_upload_dead_letter_file(work_dir: Path, job_id: str) -> Path:
    return result_upload_dead_letter_dir(work_dir) / f"{safe_job_id(job_id)}.json"

def failed_checkout_marker(checkout_dir: Path) -> Path:
    return checkout_dir.parent / f"{checkout_dir.name}{_FAILED_CHECKOUT_MARKER_SUFFIX}"


def write_failed_checkout_marker(marker: Path, expires_at: int) -> None:
    write_no_follow_text_file(marker, str(int(expires_at)))


def path_is_symlink_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False

def write_no_follow_text_file(path: Path, text: str) -> None:
    if path.parent.is_symlink():
        raise RuntimeError(f"refusing to write through symlinked directory: {path.parent}")
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
        temp_path.replace(path)
    except Exception:
        try:
            if fd >= 0:
                os.close(fd)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def read_no_follow_text_file(path: Path, max_bytes: int | None = None) -> str:
    default_limit = _LOCAL_TEXT_FILE_MAX_BYTES
    if max_bytes is None:
        max_bytes = default_limit
    byte_limit = positive_limit_int(max_bytes, default_limit, minimum=1)
    if path_is_symlink_no_follow(path):
        raise OSError(f"refusing to follow symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if path_is_symlink_no_follow(path):
            raise OSError(f"refusing to follow symlink: {path}")
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise OSError(f"not a regular file: {path}")
        if stat_result.st_size > byte_limit:
            raise OSError(f"text file too large: {path}")
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
            raise OSError(f"text file too large: {path}")
        return data.decode("utf-8")
    finally:
        if fd >= 0:
            os.close(fd)

def append_no_follow_text_file(path: Path, text: str) -> None:
    if path.parent.is_symlink():
        raise RuntimeError(f"refusing to write through symlinked directory: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def checkout_dir_from_failed_marker(marker: Path) -> Path:
    name = marker.name
    if not name.endswith(_FAILED_CHECKOUT_MARKER_SUFFIX):
        return marker.with_suffix("")
    return marker.parent / name[: -len(_FAILED_CHECKOUT_MARKER_SUFFIX)]


def cleanup_job_checkout(checkout_dir: Path) -> None:
    if checkout_dir.is_symlink():
        checkout_dir.unlink(missing_ok=True)
        return
    remove_checkout_dir(checkout_dir)


def checkout_root_sentinel(work_dir: Path) -> Path:
    return work_dir / _CHECKOUT_ROOT_SENTINEL_NAME


def checkout_root_is_owned(work_dir: Path) -> bool:
    if work_dir.is_symlink():
        return False
    sentinel = checkout_root_sentinel(work_dir)
    if checkout_root_sentinel_is_valid(sentinel):
        return True
    entries = [path for path in work_dir.iterdir() if path.name not in _CHECKOUT_RUNTIME_DIR_NAMES]
    if entries:
        return False
    try:
        write_no_follow_text_file(sentinel, "pullwise-worker checkout root\n")
    except OSError:
        return False
    return True


def checkout_root_sentinel_is_valid(sentinel: Path) -> bool:
    try:
        if not stat.S_ISREG(sentinel.lstat().st_mode):
            return False
        return read_no_follow_text_file(sentinel) == "pullwise-worker checkout root\n"
    except (OSError, UnicodeDecodeError):
        return False


def remove_checkout_dir(checkout_dir: Path) -> None:
    if checkout_dir.is_symlink():
        raise RuntimeError(f"Refusing to remove symlinked checkout directory: {checkout_dir}")
    if not checkout_dir.exists():
        return

    root_path = os.path.abspath(os.fspath(checkout_dir))

    def checkout_cleanup_path_is_inside(path: object) -> bool:
        try:
            candidate = os.path.abspath(os.fspath(path))
            return os.path.commonpath([root_path, candidate]) == root_path
        except (OSError, TypeError, ValueError):
            return False

    def chmod_no_follow(path: object, mode_bits: int) -> None:
        try:
            stat_result = os.lstat(path)
        except OSError:
            return
        if stat.S_ISLNK(stat_result.st_mode):
            return
        mode = stat_result.st_mode | mode_bits
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass

    def retry_readonly_remove(function, path, _exc_info):
        if not checkout_cleanup_path_is_inside(path):
            raise RuntimeError(f"Refusing to chmod path outside checkout cleanup root: {path}")
        chmod_no_follow(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        parent = Path(path).parent
        if checkout_cleanup_path_is_inside(parent):
            chmod_no_follow(parent, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(path)

    shutil.rmtree(checkout_dir, onerror=retry_readonly_remove)
    if checkout_dir.exists():
        raise RuntimeError(f"Failed to remove previous checkout directory: {checkout_dir}")


def clone_repository(job: dict, checkout_dir: Path, *, limits_config: WorkerConfig | None = None) -> str:
    remove_checkout_dir(checkout_dir)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_token = job.get("clone_token")
    clone_url = str(job.get("clone_url") or "")
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = f"{worker_github_web_url()}/{repo}.git"
    clone_url = trusted_or_local_clone_url(job, clone_url, clone_token)
    if clone_token_value(clone_token):
        clone_url = trusted_clone_url_for_token(job, clone_url, clone_token)
    git_env = git_auth_env(clone_token, clone_url, job.get("repo"))
    commit = normalize_git_commit_or_pending(job.get("commit"))
    branch = normalize_git_branch(job.get("branch") or "main")
    mirror_dir = repository_mirror_dir(checkout_dir.parent, job, clone_url)
    for attempt in range(2):
        try:
            ensure_repository_mirror(mirror_dir, clone_url, git_env)
            resolved_commit, mirror_ref = fetch_repository_ref(mirror_dir, branch=branch, commit=commit, env=git_env)
            if limits_config is not None:
                enforce_repository_tree_limits(limits_config, job, mirror_dir, resolved_commit)
            clone_checkout_from_mirror(mirror_dir, checkout_dir, clone_url=clone_url, mirror_ref=mirror_ref)
            touch_repository_mirror(mirror_dir)
            break
        except RepositoryTooLargeError:
            remove_checkout_dir(checkout_dir)
            raise
        except RuntimeError:
            remove_checkout_dir(checkout_dir)
            if attempt == 0:
                if mirror_dir.is_symlink():
                    raise
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
    root = work_dir.resolve(strict=False)
    cache_root = root / _REPO_CACHE_DIR_NAME
    mirror_dir = cache_root / f"{slug}-{digest}.git"
    try:
        common = os.path.commonpath([str(cache_root), str(mirror_dir)])
    except ValueError as exc:
        raise RuntimeError("repository mirror cache path must stay inside checkout root") from exc
    if os.path.normcase(common) != os.path.normcase(str(cache_root)) or mirror_dir == cache_root:
        raise RuntimeError("repository mirror cache path must stay inside checkout root")
    return mirror_dir


def ensure_repository_mirror(mirror_dir: Path, clone_url: str, env: dict[str, str] | None) -> None:
    cache_root = mirror_dir.parent
    if cache_root.is_symlink():
        raise RuntimeError("repository mirror cache root must not be a symlink")
    if cache_root.exists() and not cache_root.is_dir():
        raise RuntimeError("repository mirror cache root must be a directory")
    cache_root.mkdir(parents=True, exist_ok=True)
    if mirror_dir.is_symlink():
        raise RuntimeError("repository mirror directory must not be a symlink")
    if mirror_dir.exists() and not mirror_dir.is_dir():
        raise RuntimeError("repository mirror directory must be a directory")
    if not (mirror_dir / "HEAD").exists():
        run_git_command(["git", "init", "--bare", str(mirror_dir)], phase="mirror-init")
        run_git_command(["git", "-C", str(mirror_dir), "remote", "add", "origin", clone_url], phase="mirror-remote")
    else:
        run_git_command(["git", "-C", str(mirror_dir), "remote", "set-url", "origin", clone_url], phase="mirror-remote")


def touch_repository_mirror(mirror_dir: Path) -> None:
    try:
        now = time.time()
        os.utime(mirror_dir, (now, now), follow_symlinks=False)
    except (NotImplementedError, OSError):
        return

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


def normalize_git_commit_or_pending(value: object) -> str:
    commit = str(value or "pending").strip()
    if not commit or commit.lower() == "pending":
        return "pending"
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("Worker job commit must be a 40-character SHA or pending.")
    return commit.lower()


def normalize_git_branch(value: object) -> str:
    branch = str(value or "main").strip()
    if not branch:
        raise RuntimeError("Worker job branch is required.")
    if branch.startswith(("/", "-")) or branch.endswith(("/", ".")):
        raise RuntimeError("Worker job branch name is invalid.")
    forbidden = ("..", "@{", "\\")
    if any(item in branch for item in forbidden):
        raise RuntimeError("Worker job branch name is invalid.")
    if any(ord(char) < 32 or char in " ~^:?*[" for char in branch):
        raise RuntimeError("Worker job branch name is invalid.")
    if any(part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/")):
        raise RuntimeError("Worker job branch name is invalid.")
    return branch


def clone_checkout_from_mirror(mirror_dir: Path, checkout_dir: Path, *, clone_url: str, mirror_ref: str) -> None:
    checkout_ref = "refs/pullwise/checkout"
    run_git_command(
        ["git", "clone", "--no-checkout", str(mirror_dir), str(checkout_dir)],
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
    text = clean_protocol_text(text, 1000)
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
    output_limit = env_int(
        "PULLWISE_GIT_OUTPUT_MAX_BYTES",
        64 * 1024,
        minimum=1024,
        maximum=2 * 1024 * 1024,
    )
    log_worker_git_event(phase, "start", command=command, detail=f"timeout={timeout_seconds}s")
    try:
        with tempfile.TemporaryDirectory(prefix="pullwise-git-") as tmp_dir:
            stdout_path = Path(tmp_dir) / "git.stdout"
            stderr_path = Path(tmp_dir) / "git.stderr"
            with open_git_output_file(stdout_path) as stdout_file, open_git_output_file(
                stderr_path
            ) as stderr_file:
                completed = subprocess.run(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout_seconds,
                    env=env,
                )
            stdout_text = git_output_text(stdout_path, max_bytes=output_limit)
            stderr_text = git_output_text(stderr_path, max_bytes=output_limit)
            if completed.returncode != 0:
                message = git_error_summary(
                    phase,
                    completed.returncode,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                )
                log_worker_git_event(phase, "failed", command=command, detail=message)
                raise RuntimeError(message)
        log_worker_git_event(phase, "done", command=command)
        return stdout_text
    except subprocess.TimeoutExpired as exc:
        log_worker_git_event(phase, "timeout", command=command, detail=f"timeout={timeout_seconds}s")
        raise RuntimeError(f"git {phase} timed out after {timeout_seconds}s") from exc


def open_git_output_file(path: Path):
    if path.parent.is_symlink():
        raise OSError(f"refusing to create git output through symlinked directory: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        return os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise


def git_output_text(path: Path, *, max_bytes: int) -> str:
    try:
        with open_git_output_file_for_read(path) as handle:
            data = handle.read(max(1, int(max_bytes or 1)) + 1)
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def open_git_output_file_for_read(path: Path):
    if path.parent.is_symlink():
        raise OSError(f"refusing to read git output through symlinked directory: {path.parent}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def git_error_summary(phase: str, returncode: int, *, stdout_text: str = "", stderr_text: str = "") -> str:
    output = "\n".join(part for part in (stderr_text, stdout_text) if part)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " ".join(lines[:3])[:400]
    if not summary:
        summary = f"git exited with status {returncode}"
    return f"git {phase} failed: {summary}"


def git_error_message(phase: str, exc: subprocess.CalledProcessError) -> str:
    return git_error_summary(phase, exc.returncode, stdout_text=exc.stdout or "", stderr_text=exc.stderr or "")


def clone_token_value(clone_token: object) -> str:
    token = clone_token.get("token") if isinstance(clone_token, dict) else None
    value = str(token or "").strip()
    if any(char in value for char in "\r\n\x00") or len(value) > 4096:
        raise RuntimeError("Clone token is invalid.")
    return value


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
    if parsed.scheme == "http" and not env_bool("PULLWISE_ALLOW_INSECURE_GITHUB_WEB_URL", False):
        raise RuntimeError("PULLWISE_GITHUB_WEB_URL must use HTTPS unless PULLWISE_ALLOW_INSECURE_GITHUB_WEB_URL=1.")
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
    if parsed.scheme == "http" and not env_bool("PULLWISE_ALLOW_INSECURE_GITHUB_WEB_URL", False):
        raise RuntimeError("Repository clone URL must use HTTPS unless PULLWISE_ALLOW_INSECURE_GITHUB_WEB_URL=1.")
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


def trusted_or_local_clone_url(job: dict | None, clone_url: object, clone_token: object = None) -> str:
    text = str(clone_url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"}:
        return trusted_clone_url_for_token(job, text, clone_token)
    if clone_token_value(clone_token):
        return trusted_clone_url_for_token(job, text, clone_token)
    if env_bool("PULLWISE_ALLOW_LOCAL_CLONE_URLS", False):
        if not text or any(char in text for char in "\r\n"):
            raise RuntimeError("Repository clone URL must not be empty or contain newlines.")
        return text
    raise RuntimeError("Repository clone URL must be an HTTP(S) GitHub URL.")


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


_PACKAGE_SCRIPT_NAMES = ["dev", "start", "build", "test", "lint", "typecheck", "check"]
_REPO_CACHE_DIR_NAME = ".pullwise-repo-cache"
_RESULT_UPLOAD_DIR_NAME = ".pullwise-result-uploads"
_RESULT_UPLOAD_DEAD_LETTER_DIR_NAME = "dead-letter"
_CHECKOUT_RUNTIME_DIR_NAMES.add(_REPO_CACHE_DIR_NAME)
_CHECKOUT_RUNTIME_DIR_NAMES.add(_RESULT_UPLOAD_DIR_NAME)
_LOCAL_TEXT_FILE_MAX_BYTES = 2 * 1024 * 1024
_PENDING_RESULT_UPLOAD_RECORD_MAX_BYTES = _LOCAL_TEXT_FILE_MAX_BYTES
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
_REPOSITORY_TEXT_READ_MAX_BYTES = 512 * 1024
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

__all__ = [name for name in globals() if name == "__version__" or not (name.startswith("__") and name.endswith("__"))]
