from __future__ import annotations

# Loaded by main.py; keep definitions in that module's globals for compatibility.

import argparse
import base64
import concurrent.futures
import copy
import ctypes
import hashlib
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock

from . import __version__


PHASE_PROGRESS = {
    "clone": 10,
    "index": 25,
    "secrets": 40,
    "deps": 55,
    "ai": 80,
    "report": 95,
}
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FAILED_CHECKOUT_MARKER_SUFFIX = ".failed-retain"
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_MIN_READY_DISK_BYTES = 1024 * 1024 * 1024
_DEFAULT_MAX_REPO_FILES = 2000
_DEFAULT_MAX_REPO_BYTES = 50 * 1024 * 1024
_MIN_NODE_MAJOR = 20
_CODEX_SKIP_GIT_REPO_CHECK_ARG = "--skip-git-repo-check"
_VERIFIER_HOME_DIR_NAME = ".verifier-home"
_VERIFIER_TMP_DIR_NAME = ".verifier-tmp"
_CHECKOUT_ROOT_SENTINEL_NAME = ".pullwise-checkout-root"
_CHECKOUT_RUNTIME_DIR_NAMES = {_VERIFIER_HOME_DIR_NAME, _VERIFIER_TMP_DIR_NAME}
_PROC_MEMINFO_PATH = "/proc/meminfo"
DEFAULT_MACHINE_METRICS_INTERVAL_SECONDS = 10
WORKER_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_WORKER_PACKAGE_BASE_URL = "https://github.com/GoPullwise/pullwise-worker/releases/download"
SUPPORTED_REVIEW_PROVIDERS = {"codex"}
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
DEFAULT_SERVICE_NAME = "pullwise-worker"
DEFAULT_SERVICE_USER = "pullwise-worker"
DEFAULT_SERVICE_HOME = "/var/lib/pullwise-worker"
DEFAULT_SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_CODEX_COMMAND = f"{DEFAULT_SERVICE_HOME}/.codex/bin/codex"
DEFAULT_PROVIDER_AUTH_PATH = (
    f"{DEFAULT_SERVICE_HOME}/.local/bin:{DEFAULT_SERVICE_HOME}/.codex/bin:{DEFAULT_SERVICE_PATH}"
)
PROVIDER_ENV_PASSTHROUGH_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
AUDIT_SWARM_PROTOCOL_VERSION = "audit-swarm/0.1"
CONVERGENCE_PROTOCOL_VERSION = "pullwise-convergence/0.1"
REVIEW_DECISION_EVENT_PROTOCOL_VERSION = "pullwise-review-decision/0.1"
REVIEW_CALIBRATION_PROTOCOL_VERSION = "pullwise-review-calibration/0.2"
REVIEW_SCORING_PROTOCOL_VERSION = "pullwise-review-score/0.1"
CONVERGENCE_MIN_VERIFIED_CONFIDENCE = 0.75
CONVERGENCE_MIN_UNVERIFIED_CONFIDENCE = 0.85
REPOSITORY_TOO_LARGE_ERROR_CODE = "REPOSITORY_TOO_LARGE"
AUDIT_SWARM_EVIDENCE_BLOCK_KINDS = {
    "summary",
    "claim",
    "code_location",
    "evidence",
    "command",
    "verifier_verdict",
    "false_positive_check",
    "invariant",
    "risk",
}
CODEX_LOGIN_COMMAND = (
    f"sudo -u {DEFAULT_SERVICE_USER} env HOME={DEFAULT_SERVICE_HOME} "
    f"USERPROFILE={DEFAULT_SERVICE_HOME} "
    f"CODEX_HOME={DEFAULT_SERVICE_HOME}/.codex "
    f"XDG_CONFIG_HOME={DEFAULT_SERVICE_HOME}/.config "
    f"XDG_CACHE_HOME={DEFAULT_SERVICE_HOME}/.cache "
    f"XDG_DATA_HOME={DEFAULT_SERVICE_HOME}/.local/share "
    f"PATH={DEFAULT_PROVIDER_AUTH_PATH} "
    f"sh -lc 'cd \"$HOME\" && exec {DEFAULT_CODEX_COMMAND} login --device-auth'"
)
_CODEX_AUTH_FAILURE_MARKERS = (
    "401 Unauthorized",
    "Failed to refresh token",
    "Please log out and sign in again",
    "access token could not be refreshed",
    "refresh token was already used",
)
_CODEX_EXEC_LOCK = Lock()
_CODEX_AUTH_FAILURE_LOCK = Lock()
_codex_auth_failure_until = 0.0
_codex_auth_failure_detail = ""


def service_user_command(config: WorkerConfig | None, command: list[str]) -> str:
    service_user = str(getattr(config, "service_user", None) or DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
    service_home = str(getattr(config, "service_home", None) or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    path = provider_tool_path(config)
    quoted_command = " ".join(shlex.quote(str(part)) for part in command if str(part))
    shell_command = f'cd "$HOME" && exec {quoted_command}'
    return (
        f"sudo -u {shlex.quote(service_user)} env "
        f"HOME={shlex.quote(service_home)} "
        f"USERPROFILE={shlex.quote(service_home)} "
        f"CODEX_HOME={shlex.quote(str(Path(service_home) / '.codex'))} "
        f"XDG_CONFIG_HOME={shlex.quote(str(Path(service_home) / '.config'))} "
        f"XDG_CACHE_HOME={shlex.quote(str(Path(service_home) / '.cache'))} "
        f"XDG_DATA_HOME={shlex.quote(str(Path(service_home) / '.local' / 'share'))} "
        f"PATH={shlex.quote(path)} "
        f"sh -lc {shlex.quote(shell_command)}"
    )


def provider_tool_path(config: WorkerConfig | None) -> str:
    service_home = str(getattr(config, "service_home", None) or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    service_path = str(getattr(config, "service_path", None) or DEFAULT_SERVICE_PATH).strip() or DEFAULT_SERVICE_PATH
    path_parts = [
        f"{service_home}/.local/bin",
        f"{service_home}/.codex/bin",
        service_path,
    ]
    return ":".join(dict.fromkeys(part for part in path_parts if part))


def provider_home_path(service_home: str, *parts: str) -> str:
    home = str(service_home or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    if home.startswith("/"):
        return "/".join([home.rstrip("/"), *(part.strip("/") for part in parts if part)])
    return str(Path(home).joinpath(*parts))


def provider_process_env(config: WorkerConfig) -> dict[str, str]:
    service_home = str(config.service_home or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    env = {
        key: os.environ[key]
        for key in PROVIDER_ENV_PASSTHROUGH_KEYS
        if os.environ.get(key)
    }
    env.update(
        {
            "HOME": service_home,
            "USERPROFILE": service_home,
            "CODEX_HOME": provider_home_path(service_home, ".codex"),
            "XDG_CONFIG_HOME": provider_home_path(service_home, ".config"),
            "XDG_CACHE_HOME": provider_home_path(service_home, ".cache"),
            "XDG_DATA_HOME": provider_home_path(service_home, ".local", "share"),
            "PATH": provider_tool_path(config),
        }
    )
    return env


def codex_login_command(config: WorkerConfig) -> str:
    return service_user_command(config, [config.codex_command, "login", "--device-auth"])


def default_provider_command(service_home: str, provider: str) -> str:
    home = str(service_home or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    return f"{home.rstrip('/')}/.{provider}/bin/{provider}"


def parse_provider_chain(value: str | None, fallback: str = "") -> list[str]:
    raw = value if value is not None else fallback
    providers: list[str] = []
    for item in str(raw or "").split(","):
        provider = item.strip().lower()
        if provider in SUPPORTED_REVIEW_PROVIDERS and provider not in providers:
            providers.append(provider)
    return providers


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _VERIFIER_DISABLED_VALUES


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def metric_percent(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(max(0.0, min(100.0, (float(numerator) / float(denominator)) * 100.0)), 1)


def linux_memory_bytes() -> tuple[int | None, int | None]:
    try:
        with open(_PROC_MEMINFO_PATH, "r", encoding="utf-8") as meminfo:
            values: dict[str, int] = {}
            for line in meminfo:
                key, _, rest = line.partition(":")
                amount = rest.strip().split(" ", 1)[0]
                if amount.isdigit():
                    values[key] = int(amount) * 1024
    except OSError:
        return None, None

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
    return total, available


def windows_memory_bytes() -> tuple[int | None, int | None]:
    if platform.system().lower() != "windows":
        return None, None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
    except (AttributeError, OSError):
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def sysconf_memory_bytes() -> tuple[int | None, int | None]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None, None
    total = page_size * page_count if page_size > 0 and page_count > 0 else None
    return total, None


def worker_memory_payload() -> dict:
    total, available = linux_memory_bytes()
    if total is None:
        total, available = windows_memory_bytes()
    if total is None:
        total, available = sysconf_memory_bytes()

    used = total - available if total is not None and available is not None else None
    return {
        "totalBytes": total,
        "availableBytes": available,
        "usedBytes": used,
        "usedPercent": metric_percent(used, total),
    }


def worker_load_average_payload() -> dict | None:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        return None
    return {
        "oneMinute": round(float(one), 2),
        "fiveMinute": round(float(five), 2),
        "fifteenMinute": round(float(fifteen), 2),
    }


def worker_cpu_payload() -> dict:
    return {
        "logicalCount": os.cpu_count(),
        "loadAverage": worker_load_average_payload(),
    }


def existing_storage_path(path: str) -> str:
    candidate = os.path.abspath(path or os.getcwd())
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return candidate or os.path.abspath(os.getcwd())


def worker_storage_payload(path: str) -> dict:
    requested_path = os.path.abspath(path or os.getcwd())
    measured_path = existing_storage_path(requested_path)
    try:
        usage = shutil.disk_usage(measured_path)
    except OSError:
        return {
            "path": requested_path,
            "measuredPath": measured_path,
            "totalBytes": None,
            "usedBytes": None,
            "freeBytes": None,
            "usedPercent": None,
        }

    used = usage.total - usage.free
    return {
        "path": requested_path,
        "measuredPath": measured_path,
        "totalBytes": int(usage.total),
        "usedBytes": int(used),
        "freeBytes": int(usage.free),
        "usedPercent": metric_percent(used, usage.total),
    }


def worker_machine_metrics_payload(*, storage_path: str, timestamp: int | None = None) -> dict:
    return {
        "ok": True,
        "collectedAt": int(timestamp if timestamp is not None else time.time()),
        "worker": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "pythonVersion": platform.python_version(),
            "processId": os.getpid(),
        },
        "cpu": worker_cpu_payload(),
        "memory": worker_memory_payload(),
        "storage": worker_storage_payload(storage_path),
    }


def parse_verifier_scripts(value: str | None) -> list[str]:
    raw_items = value.split(",") if value else _VERIFIER_DEFAULT_SCRIPTS
    scripts = []
    for item in raw_items:
        script = item.strip()
        if script in _PACKAGE_SCRIPT_NAMES and script not in scripts:
            scripts.append(script)
    return scripts or list(_VERIFIER_DEFAULT_SCRIPTS)


def server_url_allowed(server_url: str, *, allow_insecure: bool = False) -> bool:
    parsed = urllib.parse.urlparse(server_url)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme != "http" or not parsed.netloc:
        return False
    if allow_insecure:
        return True
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


class WorkerConfig:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        require_worker_token: bool = True,
        validate_server_url: bool = True,
    ) -> None:
        self.server_url = (getattr(args, "server_url", None) or os.environ.get("PULLWISE_SERVER_URL") or "http://localhost:8080").rstrip("/")
        self.allow_insecure_server_url = env_bool("PULLWISE_ALLOW_INSECURE_SERVER_URL", False)
        if validate_server_url and not server_url_allowed(self.server_url, allow_insecure=self.allow_insecure_server_url):
            raise ValueError(
                "PULLWISE_SERVER_URL must use https unless it points to localhost/127.0.0.1 "
                "or PULLWISE_ALLOW_INSECURE_SERVER_URL=true is set."
            )
        self.worker_token = getattr(args, "worker_token", None) or os.environ.get("PULLWISE_WORKER_TOKEN") or ""
        self.worker_id = getattr(args, "worker_id", None) or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        configured_provider = str(getattr(args, "provider", None) or os.environ.get("PULLWISE_PROVIDER") or "").strip().lower()
        self.provider_chain = parse_provider_chain(
            os.environ.get("PULLWISE_PROVIDER_CHAIN"),
            configured_provider,
        )
        self.provider = configured_provider or (self.provider_chain[0] if self.provider_chain else "")
        self.max_concurrent_jobs = max(1, int(getattr(args, "max_concurrent_jobs", None) or os.environ.get("PULLWISE_MAX_CONCURRENT_JOBS") or 1))
        self.max_claim_jobs = max(1, int(getattr(args, "max_claim_jobs", None) or os.environ.get("PULLWISE_WORKER_MAX_CLAIM_JOBS") or 2))
        self.poll_seconds = max(1, int(getattr(args, "poll_seconds", None) or os.environ.get("PULLWISE_WORKER_POLL_SECONDS") or 5))
        self.poll_jitter_seconds = max(0.0, float(os.environ.get("PULLWISE_WORKER_POLL_JITTER_SECONDS") or 2))
        self.max_backoff_seconds = max(self.poll_seconds, int(os.environ.get("PULLWISE_WORKER_MAX_BACKOFF_SECONDS") or 60))
        checkout_root = getattr(args, "checkout_root", None) or os.environ.get("PULLWISE_CHECKOUT_ROOT")
        work_dir = getattr(args, "work_dir", None) or os.environ.get("PULLWISE_WORKER_WORK_DIR")
        self.work_dir = Path(checkout_root) if checkout_root else Path(work_dir or tempfile.gettempdir()) / "pullwise-worker"
        log_dir = getattr(args, "log_dir", None) or os.environ.get("PULLWISE_LOG_DIR")
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir()) / "pullwise-worker-logs"
        self.service_user = os.environ.get("PULLWISE_SERVICE_USER", DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
        self.service_home = os.environ.get("PULLWISE_SERVICE_HOME", DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
        self.service_path = os.environ.get("PULLWISE_SERVICE_PATH", DEFAULT_SERVICE_PATH).strip() or DEFAULT_SERVICE_PATH
        self.service_name = os.environ.get("PULLWISE_SERVICE_NAME", DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
        self.service_file = (
            os.environ.get("PULLWISE_SERVICE_FILE", f"/etc/systemd/system/{self.service_name}.service").strip()
            or f"/etc/systemd/system/{self.service_name}.service"
        )
        self.worker_env_file = (
            os.environ.get("PULLWISE_WORKER_ENV_FILE", "/etc/pullwise-worker/worker.env").strip()
            or "/etc/pullwise-worker/worker.env"
        )
        self.worker_env_backup_file = (
            os.environ.get("PULLWISE_WORKER_ENV_BACKUP_FILE", f"{self.worker_env_file}.bak").strip()
            or f"{self.worker_env_file}.bak"
        )
        self.worker_bin_path = (
            os.environ.get("PULLWISE_WORKER_BIN_PATH", "/usr/local/bin/pullwise-worker").strip()
            or "/usr/local/bin/pullwise-worker"
        )
        self.logrotate_file = (
            os.environ.get("PULLWISE_LOGROTATE_FILE", f"/etc/logrotate.d/{self.service_name}").strip()
            or f"/etc/logrotate.d/{self.service_name}"
        )
        self.lifecycle_watcher_enabled = env_bool("PULLWISE_LIFECYCLE_WATCHER_ENABLED", False)
        self.watcher_poll_seconds = max(1, int(os.environ.get("PULLWISE_WATCHER_POLL_SECONDS") or self.poll_seconds))
        self.watcher_service_name = (
            os.environ.get("PULLWISE_WATCHER_SERVICE_NAME", f"{self.service_name}-watcher").strip()
            or f"{self.service_name}-watcher"
        )
        self.watcher_service_file = (
            os.environ.get("PULLWISE_WATCHER_SERVICE_FILE", f"/etc/systemd/system/{self.watcher_service_name}.service").strip()
            or f"/etc/systemd/system/{self.watcher_service_name}.service"
        )
        self.remote_uninstall_finalizer = env_bool("PULLWISE_REMOTE_UNINSTALL_FINALIZER", False)
        self.uninstall_marker_file = (
            os.environ.get("PULLWISE_UNINSTALL_MARKER_FILE", f"/run/{self.service_name}/uninstall-requested").strip()
            or f"/run/{self.service_name}/uninstall-requested"
        )
        default_codex_command = default_provider_command(self.service_home, "codex")
        self.codex_command = getattr(args, "codex_command", None) or os.environ.get("PULLWISE_CODEX_COMMAND") or default_codex_command
        self.codex_model = os.environ.get("PULLWISE_CODEX_MODEL", DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL
        self.codex_reasoning_effort = (
            os.environ.get("PULLWISE_CODEX_REASONING_EFFORT", DEFAULT_CODEX_REASONING_EFFORT).strip()
            or DEFAULT_CODEX_REASONING_EFFORT
        )
        self.codex_timeout_seconds = max(60, int(getattr(args, "codex_timeout_seconds", None) or os.environ.get("PULLWISE_CODEX_TIMEOUT_SECONDS") or 1800))
        self.codex_doctor_timeout_seconds = max(10, int(os.environ.get("PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS") or 60))
        self.codex_auth_failure_cooldown_seconds = max(0, int(os.environ.get("PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS") or 3600))
        self.readiness_check_seconds = max(10, int(os.environ.get("PULLWISE_READINESS_CHECK_SECONDS") or 60))
        self.machine_metrics_interval_seconds = env_int(
            "PULLWISE_WORKER_MACHINE_METRICS_SECONDS",
            DEFAULT_MACHINE_METRICS_INTERVAL_SECONDS,
            minimum=1,
        )
        self.result_upload_attempts = max(1, int(os.environ.get("PULLWISE_RESULT_UPLOAD_ATTEMPTS") or 5))
        self.failed_checkout_retention_seconds = max(0, int(os.environ.get("PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS") or 0))
        self.max_checkout_bytes = max(1, int(os.environ.get("PULLWISE_MAX_CHECKOUT_BYTES") or 20 * 1024 * 1024 * 1024))
        self.max_repo_files = _DEFAULT_MAX_REPO_FILES
        self.max_repo_bytes = _DEFAULT_MAX_REPO_BYTES
        self.semantic_graph_agent_fallback = env_bool("PULLWISE_SEMANTIC_GRAPH_AGENT_FALLBACK", False)
        self.semantic_graph_agent_min_symbols = env_int("PULLWISE_SEMANTIC_GRAPH_AGENT_MIN_SYMBOLS", 8, minimum=0)
        self.semantic_graph_agent_timeout_seconds = max(
            30,
            int(os.environ.get("PULLWISE_SEMANTIC_GRAPH_AGENT_TIMEOUT_SECONDS") or 180),
        )
        self.cleanup_interval_seconds = max(60, int(os.environ.get("PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS") or 3600))
        self.log_retention_seconds = max(0, int(os.environ.get("PULLWISE_LOG_RETENTION_SECONDS") or 14 * 24 * 60 * 60))
        self.max_log_bytes = max(1, int(os.environ.get("PULLWISE_MAX_LOG_BYTES") or 1024 * 1024 * 1024))
        self.scan_summary_log_max_bytes = max(1024, int(os.environ.get("PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES") or 10 * 1024 * 1024))
        self.verifier_enabled = env_bool("PULLWISE_WORKER_VERIFIER_ENABLED", False)
        self.verifier_install_deps = env_bool("PULLWISE_WORKER_VERIFIER_INSTALL_DEPS", True)
        self.verifier_confirm_failures = env_bool("PULLWISE_WORKER_VERIFIER_CONFIRM_FAILURES", True)
        self.verifier_host_execution_allowed = env_bool("PULLWISE_WORKER_VERIFIER_ALLOW_HOST_EXECUTION", False)
        self.verifier_timeout_seconds = max(10, int(os.environ.get("PULLWISE_WORKER_VERIFIER_TIMEOUT_SECONDS") or 120))
        self.verifier_max_commands = max(1, int(os.environ.get("PULLWISE_WORKER_VERIFIER_MAX_COMMANDS") or 5))
        self.verifier_scripts = parse_verifier_scripts(os.environ.get("PULLWISE_WORKER_VERIFIER_SCRIPTS"))
        self.review_calibration_mode = (
            os.environ.get("PULLWISE_REVIEW_CALIBRATION_MODE", "shadow").strip().lower() or "shadow"
        )
        if self.review_calibration_mode not in {"off", "shadow", "audit_only", "enforce"}:
            self.review_calibration_mode = "shadow"
        self.review_calibration_model = (
            os.environ.get("PULLWISE_REVIEW_CALIBRATION_MODEL", "relative_factor").strip().lower()
            or "relative_factor"
        )
        if self.review_calibration_model not in {"relative_factor", "logit_beta"}:
            self.review_calibration_model = "relative_factor"
        self.review_calibration_half_life_days = env_float(
            "PULLWISE_REVIEW_CALIBRATION_HALF_LIFE_DAYS",
            45.0,
            minimum=1.0,
        )
        self.review_calibration_min_effective_samples = env_int(
            "PULLWISE_REVIEW_CALIBRATION_MIN_EFFECTIVE_SAMPLES",
            20,
            minimum=1,
        )
        self.review_calibration_enable_buckets = env_bool("PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS", False)
        self.review_calibration_enable_hierarchy = env_bool("PULLWISE_REVIEW_CALIBRATION_ENABLE_HIERARCHY", False)
        self.review_calibration_enable_drift = env_bool("PULLWISE_REVIEW_CALIBRATION_ENABLE_DRIFT", False)
        self.review_calibration_sample_audit_rate = env_float(
            "PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE",
            0.02,
            minimum=0.0,
            maximum=1.0,
        )
        self.review_calibration_borderline_sample_window = env_float(
            "PULLWISE_REVIEW_CALIBRATION_BORDERLINE_SAMPLE_WINDOW",
            0.03,
            minimum=0.0,
            maximum=0.10,
        )
        if require_worker_token and not self.worker_token:
            raise ValueError("PULLWISE_WORKER_TOKEN is required")


class PullwiseRequestError(Exception):
    pass


class PullwiseHTTPError(PullwiseRequestError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class PullwiseResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PullwiseRequestError(f"invalid JSON response: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}


class PullwiseClient:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.headers = {
            "Authorization": f"Bearer {config.worker_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, path: str, payload: dict) -> PullwiseResponse:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.server_url}{path}",
            data=body,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=WORKER_HTTP_TIMEOUT_SECONDS) as response:
                return PullwiseResponse(response.read())
        except urllib.error.HTTPError as exc:
            reason = getattr(exc, "reason", None) or getattr(exc, "msg", "") or "error"
            raise PullwiseHTTPError(f"HTTP {exc.code}: {reason}", exc.code) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise PullwiseRequestError(str(exc)) from exc

    def delete(self, path: str) -> PullwiseResponse:
        request = urllib.request.Request(
            f"{self.config.server_url}{path}",
            headers=self.headers,
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=WORKER_HTTP_TIMEOUT_SECONDS) as response:
                return PullwiseResponse(response.read())
        except urllib.error.HTTPError as exc:
            reason = getattr(exc, "reason", None) or getattr(exc, "msg", "") or "error"
            raise PullwiseHTTPError(f"HTTP {exc.code}: {reason}", exc.code) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise PullwiseRequestError(str(exc)) from exc

    def heartbeat(
        self,
        *,
        running_jobs: int = 0,
        active_job_ids: list[str] | None = None,
        last_error: str | None = None,
        doctor_status: str | None = None,
        codex_ready: bool | None = None,
        ready_providers: list[str] | None = None,
        systemd_active: bool | None = None,
        doctor_checked_at: int | None = None,
        machine_metrics: dict | None = None,
    ) -> dict:
        payload = {
            "worker_id": self.config.worker_id,
            "version": __version__,
            "provider": self.config.provider,
            "providerChain": list(self.config.provider_chain),
            "max_concurrent_jobs": self.config.max_concurrent_jobs,
            "running_jobs": running_jobs,
            "free_slots": max(0, self.config.max_concurrent_jobs - running_jobs),
            "hostname": socket.gethostname(),
            "last_error": last_error,
            "doctor_status": doctor_status,
            "codex_ready": codex_ready,
            "systemd_active": systemd_active,
            "doctor_checked_at": doctor_checked_at,
        }
        if ready_providers is not None:
            payload["readyProviders"] = [str(provider) for provider in ready_providers if str(provider or "").strip()]
        if active_job_ids is not None:
            payload["active_job_ids"] = [str(job_id) for job_id in active_job_ids if str(job_id or "").strip()]
        if isinstance(machine_metrics, dict):
            payload["machine_metrics"] = machine_metrics
        response = self.post("/worker/heartbeat", payload)
        return response.json()

    def agent_configs(self) -> dict:
        response = self.post("/worker/agent-configs", {"worker_id": self.config.worker_id})
        return response.json()

    def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
        payload = {"worker_id": self.config.worker_id, "status": status}
        if error:
            payload["error"] = error
        self.post(f"/worker/commands/{command_id}/status", payload)

    def command_poll(self) -> dict:
        response = self.post("/worker/commands/poll", {"worker_id": self.config.worker_id})
        return response.json()

    def claim(self) -> dict | None:
        response = self.post(
            "/worker/jobs/claim",
            {"worker_id": self.config.worker_id, "max_jobs": min(self.config.max_concurrent_jobs, self.config.max_claim_jobs)},
        )
        return response.json().get("job")

    def claim_many(self, max_jobs: int) -> list[dict]:
        response = self.post("/worker/jobs/claim", {"worker_id": self.config.worker_id, "max_jobs": max_jobs})
        data = response.json()
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            return [job for job in jobs if isinstance(job, dict)]
        job = data.get("job")
        return [job] if isinstance(job, dict) else []

    def progress(
        self,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
        audit_swarm: dict | None = None,
        repositoryGraph: dict | None = None,
        semanticGraph: dict | None = None,
        impactGraph: dict | None = None,
        completion_audit: dict | None = None,
        job_trace: dict | None = None,
    ) -> None:
        payload = {
            "phase": phase,
            "progress": progress,
            "message": message,
            "started_at": int(time.time()),
            "logs_summary": logs_summary,
        }
        if audit_swarm:
            payload["audit_swarm"] = audit_swarm
        if repositoryGraph:
            payload["repositoryGraph"] = repositoryGraph
        if semanticGraph:
            payload["semanticGraph"] = semanticGraph
        if impactGraph:
            payload["impactGraph"] = impactGraph
        if completion_audit:
            payload["completion_audit"] = completion_audit
            payload["completionAudit"] = completion_audit
        if job_trace:
            payload["job_trace"] = job_trace
            payload["jobTrace"] = job_trace
        self.post(f"/worker/jobs/{job_id}/progress", payload)

    def result(self, job_id: str, payload: dict) -> None:
        self.post(f"/worker/jobs/{job_id}/result", payload)


def unregister_worker_from_server(config: WorkerConfig, *, dry_run: bool = False) -> bool:
    if not config.worker_token:
        if dry_run:
            print("skip server registry unregister: PULLWISE_WORKER_TOKEN is not set")
        return False
    if dry_run:
        print(f"DELETE {config.server_url}/worker/registry")
        return True
    PullwiseClient(config).delete("/worker/registry")
    return True


def uninstall_worker_command(args: argparse.Namespace) -> int:
    try:
        config = WorkerConfig(args, require_worker_token=False, validate_server_url=False)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        unregister_worker_from_server(config, dry_run=args.dry_run)
    except PullwiseRequestError as exc:
        print(f"server registry unregister failed: {redact_secrets(str(exc), config)}", file=sys.stderr)
        return 1
    return uninstall_worker(
        config,
        remove_config=args.remove_config,
        remove_logs=args.remove_logs,
        dry_run=args.dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Pullwise pull worker.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run",
            "doctor",
            "start",
            "stop",
            "status",
            "restart",
            "update",
            "uninstall",
            "finalize-uninstall",
            "watch",
            "cleanup",
        ],
    )
    parser.add_argument("--server-url")
    parser.add_argument("--worker-id")
    parser.add_argument("--max-concurrent-jobs", type=int)
    parser.add_argument("--max-claim-jobs", type=int)
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--work-dir")
    parser.add_argument("--checkout-root")
    parser.add_argument("--log-dir")
    parser.add_argument("--provider")
    parser.add_argument("--codex-command")
    parser.add_argument("--codex-timeout-seconds", type=int)
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove-config", action="store_true")
    parser.add_argument("--remove-logs", action="store_true")
    args = parser.parse_args()

    if args.command in {"start", "stop", "status", "restart"}:
        try:
            config = WorkerConfig(args, require_worker_token=False, validate_server_url=False)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        raise SystemExit(service_action(args.command, dry_run=args.dry_run, config=config))
    if args.command == "uninstall":
        raise SystemExit(uninstall_worker_command(args))
    if args.command == "finalize-uninstall":
        try:
            config = WorkerConfig(args, require_worker_token=False, validate_server_url=False)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        raise SystemExit(finalize_worker_uninstall(config, dry_run=args.dry_run))
    if args.command == "watch":
        try:
            config = WorkerConfig(args, require_worker_token=True, validate_server_url=True)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        raise SystemExit(run_lifecycle_watcher(config, once=args.once))
    require_worker_token = args.command in {"run", "doctor"}
    try:
        config = WorkerConfig(
            args,
            require_worker_token=require_worker_token,
            validate_server_url=args.command not in {"update", "cleanup"},
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "doctor":
        raise SystemExit(0 if run_doctor(config) else 1)
    if args.command == "update":
        raise SystemExit(update_worker(config, dry_run=args.dry_run))
    if args.command == "cleanup":
        cleanup_worker_resources(config)
        raise SystemExit(0)
    worker = Worker(config)
    worker.run(once=args.once)


