from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.client = PullwiseClient(config)
        self.last_error: str | None = None
        self._readiness_checked_at = 0.0
        self._doctor_status = "not_ready"
        self._codex_ready = False
        self._empty_poll_count = 0
        self._error_poll_count = 0
        self._last_cleanup_at = 0.0

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_resources_if_due([], force=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_concurrent_jobs) as executor:
            running: dict[concurrent.futures.Future[None], dict] = {}
            while True:
                done = [future for future in running if future.done()]
                for future in done:
                    job = running.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        self.last_error = f"job {job.get('job_id')} failed unexpectedly: {exc}"[:500]
                self.cleanup_resources_if_due(running.values())
                free_slots = max(0, self.config.max_concurrent_jobs - len(running))
                ready = self.refresh_readiness_if_due()
                loop_error = False
                claimed_jobs = 0
                heartbeat_payload: dict = {}
                try:
                    heartbeat_response = self.client.heartbeat(
                        running_jobs=len(running),
                        last_error=self.last_error,
                        doctor_status=self._doctor_status,
                        codex_ready=self._codex_ready,
                        doctor_checked_at=int(self._readiness_checked_at) if self._readiness_checked_at else None,
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
                            claim_limit = 1 if once else free_slots
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
                        return
                elif once:
                    concurrent.futures.wait(running)
                    return
                time.sleep(self.next_poll_sleep(claimed_jobs=claimed_jobs, loop_error=loop_error))

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
        try:
            self.client.command_status(command_id, "running")
        except PullwiseRequestError as exc:
            self.last_error = f"command ack failed: {redact_secrets(str(exc), self.config)}"[:500]
            return False
        code = execute_lifecycle_command(action)
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

    def next_poll_sleep(self, *, claimed_jobs: int, loop_error: bool) -> float:
        if loop_error:
            self._error_poll_count += 1
            self._empty_poll_count = 0
            base = self.config.poll_seconds * (2 ** min(self._error_poll_count - 1, 6))
        elif claimed_jobs:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = self.config.poll_seconds
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
        checks, _provider_ready = worker_readiness_checks(self.config)
        failed_check = first_failed_check(checks)
        self._codex_ready = readiness_check_ok(checks, "codex_ready")
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
        attempt_id = f"{self.config.worker_id}-{job.get('attempt') or 1}"
        checkout_dir = checkout_dir_for_job(self.config.work_dir, job_id)
        started = time.monotonic()
        duration_ms = 0
        job_error = ""
        try:
            self.client.progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Cloning repository",
                audit_swarm=audit_swarm_scan_artifacts("clone", config=self.config, summary="Cloning repository."),
            )
            resolved_commit = clone_repository(job, checkout_dir)
            job["resolved_commit"] = resolved_commit
            job["commit"] = resolved_commit
            self.client.progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository ready",
                audit_swarm=audit_swarm_scan_artifacts("preflight", config=self.config, summary="Repository checkout is ready."),
            )
            preflight = collect_preflight_metadata(self.config, job, checkout_dir)
            try:
                verifier, verifier_findings, verifier_logs = run_verifier_commands(self.config, job, checkout_dir, preflight)
            except Exception as exc:
                verifier = {
                    "enabled": self.config.verifier_enabled,
                    "summary": f"Verifier failed before completing: {redact_secrets(str(exc), self.config)}"[:500],
                    "runs": [],
                }
                verifier_findings = []
                verifier_logs = verifier["summary"]
            preflight["verifier"] = verifier
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
                "Running Codex review",
                verifier_logs,
                audit_swarm=audit_swarm_scan_artifacts(
                    "discovery",
                    config=self.config,
                    preflight=preflight,
                    summary="Repository preflight is complete; reviewer agents are discovering issue cards.",
                ),
            )
            audit_payload, summary, logs_summary = run_codex_review(self.config, job, checkout_dir)
            ai_usage = normalize_ai_usage(audit_payload.get("ai_usage"))
            if verifier_findings:
                audit_payload = merge_audit_swarm_payloads(
                    audit_swarm_payload_from_findings(verifier_findings, verifier_role="verifier"),
                    audit_payload,
                )
            projected_findings = audit_swarm_findings_from_payload(audit_payload) or []
            candidate_count = len(projected_findings)
            review_decision_records: list[dict] = []
            projected_findings, rejected_reasons, rejected_samples = filter_reportable_findings(
                projected_findings,
                review_decision_records,
            )
            projected_findings, convergence_rejected_reasons, convergence_rejected_samples, convergence_state = (
                apply_convergence_gate(job, checkout_dir, projected_findings, review_decision_records)
            )
            for reason, count in convergence_rejected_reasons.items():
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + count
            rejected_samples = [*rejected_samples, *convergence_rejected_samples][:5]
            review_calibration = apply_review_calibration_decisions(
                self.config,
                job,
                projected_findings,
                review_decision_records,
                attempt_id=attempt_id,
            )
            projected_findings = review_calibration["reported_findings"]
            for reason, count in review_calibration["rejected_reasons"].items():
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + count
            rejected_samples = [*rejected_samples, *review_calibration["rejected_samples"]][:5]
            audit_payload = filter_audit_swarm_payload_by_findings(audit_payload, projected_findings)
            summary = summarize(projected_findings)
            verification_audit = verification_audit_payload(
                candidate_count=candidate_count,
                reported_findings=projected_findings,
                rejected_reasons=rejected_reasons,
                rejected_samples=rejected_samples,
                audit_only_findings=review_calibration["audit_only_findings"],
                audit_only_samples=review_calibration["audit_only_samples"],
                verified_suppression_count=review_calibration["verified_suppression_count"],
            )
            if verifier_logs:
                logs_summary = "\n".join([verifier_logs, logs_summary])[-1000:]
            duration_ms = int((time.monotonic() - started) * 1000)
            audit_swarm = audit_swarm_scan_artifacts(
                "report",
                config=self.config,
                audit_payload=audit_payload,
                preflight=preflight,
                verification_audit=verification_audit,
                summary=verification_audit.get("summary") or "Audit Swarm result is ready.",
                logs_summary=logs_summary,
            )
            payload = {
                "status": "done",
                "commit": resolved_commit,
                "resolved_commit": resolved_commit,
                "audit_protocol": audit_payload.get("audit_protocol") or AUDIT_SWARM_PROTOCOL_VERSION,
                "issue_cards": audit_payload.get("issue_cards") if isinstance(audit_payload.get("issue_cards"), list) else [],
                "verification_results": (
                    audit_payload.get("verification_results")
                    if isinstance(audit_payload.get("verification_results"), list)
                    else []
                ),
                "summary": summary,
                "duration_ms": duration_ms,
                "attempt_id": attempt_id,
                "preflight": preflight,
                "verification_audit": verification_audit,
                "convergence_state": convergence_state,
                "review_decision_events": review_calibration["decision_events"],
                "audit_swarm": audit_swarm,
            }
            if ai_usage:
                payload["ai_usage"] = ai_usage
            payload["result_checksum"] = result_checksum(payload)
            self.client.progress(job_id, "report", 100, "Uploading result", logs_summary, audit_swarm=audit_swarm)
            try:
                self.upload_result_with_retry(job_id, payload)
            except Exception as exc:
                self.last_error = f"result upload failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                job_error = self.last_error
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "done", duration_ms, "")
            self.last_error = None
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error = redact_secrets(str(exc)[:500], self.config)
            error_payload = {
                "status": "failed",
                "audit_protocol": AUDIT_SWARM_PROTOCOL_VERSION,
                "issue_cards": [],
                "verification_results": [],
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": duration_ms,
                "error": error,
                "attempt_id": attempt_id,
                "audit_swarm": audit_swarm_scan_artifacts("failed", config=self.config, summary=error),
            }
            if job.get("resolved_commit"):
                error_payload["commit"] = job["resolved_commit"]
                error_payload["resolved_commit"] = job["resolved_commit"]
            error_payload["result_checksum"] = result_checksum(error_payload)
            try:
                self.upload_result_with_retry(job_id, error_payload)
            except Exception as upload_exc:
                self.last_error = f"failed result upload failed for {job_id}: {redact_secrets(str(upload_exc), self.config)}"[:500]
                job_error = self.last_error
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "failed", duration_ms, error)
            self.last_error = error
            job_error = error
        finally:
            if job_error and self.config.failed_checkout_retention_seconds > 0:
                marker = failed_checkout_marker(checkout_dir)
                marker.write_text(str(int(time.time()) + self.config.failed_checkout_retention_seconds), encoding="utf-8")
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


def clone_repository(job: dict, checkout_dir: Path) -> str:
    shutil.rmtree(checkout_dir, ignore_errors=True)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_url = str(job.get("clone_url") or "")
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = f"https://github.com/{repo}.git"
    git_env = git_auth_env(job.get("clone_token"))
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


def git_auth_env(clone_token: object) -> dict[str, str] | None:
    token = clone_token_value(clone_token)
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
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
        "kind": "stripe_live_secret_key",
        "label": "Stripe live secret key",
        "regex": re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),
    },
    {
        "kind": "slack_token",
        "label": "Slack token",
        "regex": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    },
]


