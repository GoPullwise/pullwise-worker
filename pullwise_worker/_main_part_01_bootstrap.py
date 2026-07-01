from __future__ import annotations

# Loaded by main.py; definitions are executed in that module's globals.

import argparse
import base64
import concurrent.futures
import copy
import ctypes
import gzip
import hashlib
import json
import math
import os
import platform
import random
import re
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
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
_SAFE_WORKER_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_MAX_JOB_ID_LENGTH = 128
_FAILED_CHECKOUT_MARKER_SUFFIX = ".failed-retain"
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_MIN_READY_DISK_BYTES = 1024 * 1024 * 1024
_DEFAULT_MAX_REPO_FILES = 2000
_DEFAULT_MAX_REPO_BYTES = 50 * 1024 * 1024
_MAX_REPO_LIMIT_FILES = 20_000
_MAX_REPO_LIMIT_BYTES = 512 * 1024 * 1024
_MIN_NODE_MAJOR = 20
_CODEX_SKIP_GIT_REPO_CHECK_ARG = "--skip-git-repo-check"
_CHECKOUT_ROOT_SENTINEL_NAME = ".pullwise-checkout-root"
_CHECKOUT_RUNTIME_DIR_NAMES: set[str] = set()
_PROC_MEMINFO_PATH = "/proc/meminfo"
DEFAULT_MACHINE_METRICS_INTERVAL_SECONDS = 10
WORKER_HTTP_TIMEOUT_SECONDS = 60
WORKER_HTTP_RESPONSE_MAX_BYTES = 1024 * 1024
DEFAULT_WORKER_PACKAGE_BASE_URL = "https://github.com/GoPullwise/pullwise-worker/releases/download"
SUPPORTED_REVIEW_PROVIDERS = {"codex"}
DEFAULT_ACTIVE_READINESS_CHECK_SECONDS = 60
DEFAULT_DEGRADED_READINESS_CHECK_SECONDS = 600
DEFAULT_READINESS_CHECK_SECONDS = DEFAULT_ACTIVE_READINESS_CHECK_SECONDS
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
REVIEW_DECISION_EVENT_PROTOCOL_VERSION = "pullwise-review-decision/0.1"
WORKER_REVIEW_PROTOCOL_VERSION = "review-worker-protocol/v1"
REPOSITORY_TOO_LARGE_ERROR_CODE = "REPOSITORY_TOO_LARGE"
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
_CODEX_AUTH_EXPIRED_MARKERS = (
    "access token expired",
    "session expired",
    "token expired",
    "expired token",
    "token was revoked",
    "revoked token",
    "failed to refresh token",
    "access token could not be refreshed",
    "refresh token was already used",
    "please log out and sign in again",
)
_CODEX_AUTH_REQUIRED_MARKERS = (
    "not authenticated",
    "authentication required",
    "login required",
    "please log in",
    "please login",
    "sign in",
    "missing api key",
    "invalid api key",
    "api key required",
    "401 unauthorized",
)
_CODEX_AUTHORIZATION_MARKERS = (
    "403",
    "forbidden",
    "workspace disabled",
    "codex local disabled",
    "contact your chatgpt administrator",
    "not authorized",
    "unauthorized workspace",
)
_CODEX_SUBSCRIPTION_MARKERS = (
    "subscription expired",
    "subscription inactive",
    "subscription required",
    "plan expired",
    "plan inactive",
    "payment required",
    "billing issue",
)
_CODEX_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "quota exhausted",
    "usage limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "no credits",
    "credits exhausted",
    "out of credits",
    "429",
)
_CODEX_VERSION_MARKERS = (
    "unknown subcommand",
    "unrecognized subcommand",
    "unknown command",
    "unrecognized command",
    "unknown option",
    "unrecognized option",
    "unexpected argument",
    "invalid argument",
)
_CODEX_READINESS_ISSUE_MESSAGES = {
    "codex_auth_required": "codex_auth_required: sign in with the worker service user's Codex account",
    "codex_auth_expired": "codex_auth_expired: refresh the worker service user's Codex login",
    "codex_authorization_failed": "codex_authorization_failed: Codex account or workspace is not authorized",
    "codex_subscription_inactive": "codex_subscription_inactive: ChatGPT subscription is inactive or lacks Codex access",
    "codex_quota_exhausted": "codex_quota_exhausted: Codex usage quota or credits are exhausted",
    "codex_version_unsupported": "codex_version_unsupported: installed Codex CLI does not support the required app-server interface",
}
_CODEX_READINESS_FAILURE_CACHEABLE_ISSUES = set(_CODEX_READINESS_ISSUE_MESSAGES)
_UBUNTU_2204_DEPENDENCY_PACKAGES = {
    "git": ("git",),
    "python3": ("python3.10", "python3.10-venv"),
    "python3.10": ("python3.10", "python3.10-venv"),
    "python3-pip": ("python3-pip",),
    "runuser": ("util-linux",),
    "systemctl": ("systemd",),
    "logrotate": ("logrotate",),
}
_DEPENDENCY_INSTALL_DISABLED_VALUES = {"0", "false", "no", "off"}
_ENV_FALSE_VALUES = {"", "0", "false", "no", "off"}


def codex_readiness_issue_kind(detail: object) -> str:
    text = str(detail or "")
    if not text:
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in _CODEX_SUBSCRIPTION_MARKERS) or (
        "subscription" in lowered
        and any(marker in lowered for marker in ("expired", "inactive", "required", "renew", "payment", "billing"))
    ):
        return "codex_subscription_inactive"
    if any(marker in lowered for marker in _CODEX_QUOTA_MARKERS):
        return "codex_quota_exhausted"
    if any(marker in lowered for marker in _CODEX_AUTH_EXPIRED_MARKERS):
        return "codex_auth_expired"
    if any(marker in lowered for marker in _CODEX_AUTHORIZATION_MARKERS):
        return "codex_authorization_failed"
    if any(marker in lowered for marker in _CODEX_AUTH_REQUIRED_MARKERS):
        return "codex_auth_required"
    if "app-server" in lowered and any(marker in lowered for marker in _CODEX_VERSION_MARKERS):
        return "codex_version_unsupported"
    return ""


def codex_readiness_issue_detail(detail: object, config: object) -> str:
    kind = codex_readiness_issue_kind(detail)
    if not kind:
        return ""
    clean_detail = clean_protocol_text(redact_secrets(str(detail or ""), config), 500)
    if clean_detail.lower().startswith(f"{kind}:"):
        return clean_detail[:500]
    message = _CODEX_READINESS_ISSUE_MESSAGES[kind]
    if kind in {"codex_auth_required", "codex_auth_expired"}:
        message = f"{message}; run codex login --device-auth as the worker service user"
    if kind == "codex_authorization_failed":
        message = f"{message}; check ChatGPT workspace/admin access and Codex Local availability"
    if kind == "codex_quota_exhausted":
        message = f"{message}; check the signed-in ChatGPT plan, usage limits, or API-key billing path"
    if kind == "codex_subscription_inactive":
        message = f"{message}; renew or switch the Codex login to an account with Codex access"
    if kind == "codex_version_unsupported":
        message = f"{message}; upgrade the Codex CLI installed for the worker service user"
    if clean_detail:
        message = f"{message}; detail: {clean_detail}"
    return message[:500]


def codex_readiness_failure_cacheable(detail: object) -> bool:
    return codex_readiness_issue_kind(detail) in _CODEX_READINESS_FAILURE_CACHEABLE_ISSUES


def looks_like_codex_auth_failure(output: object) -> bool:
    kind = codex_readiness_issue_kind(output)
    if kind in {"codex_auth_required", "codex_auth_expired", "codex_authorization_failed"}:
        return True
    text = str(output or "")
    lowered = text.lower()
    return bool(text) and any(marker.lower() in lowered for marker in _CODEX_AUTH_FAILURE_MARKERS)


def mark_codex_auth_failure(config: object, detail: object) -> None:
    return None


def cached_codex_readiness_failure_detail() -> str:
    return ""


def clear_codex_auth_failure() -> None:
    return None


def auto_install_dependencies_enabled() -> bool:
    value = os.environ.get("PULLWISE_WORKER_AUTO_INSTALL_DEPS")
    return value is None or value.strip().lower() not in _DEPENDENCY_INSTALL_DISABLED_VALUES


def os_release_values(path: str = "/etc/os-release") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, sep, raw_value = line.strip().partition("=")
                if not sep or not key:
                    continue
                values[key] = raw_value.strip().strip('"')
    except OSError:
        return {}
    return values


def ubuntu_2204_host() -> bool:
    release = os_release_values()
    return release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "22.04"


def python310_available() -> bool:
    executable = shutil.which("python3.10")
    if not executable:
        return False
    completed = subprocess.run(
        [executable, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def python310_pip_available() -> bool:
    executable = shutil.which("python3.10")
    if not executable:
        return False
    completed = subprocess.run(
        [executable, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def node20_available() -> bool:
    executable = shutil.which("node")
    if not executable:
        return False
    completed = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        return False
    match = re.search(r"v?(\d+)", completed.stdout.strip())
    return bool(match and int(match.group(1)) >= _MIN_NODE_MAJOR)


def npm_available() -> bool:
    return node20_available() and shutil.which("npm") is not None


def dependency_available(name: str) -> bool:
    if name in {"python3", "python3.10"}:
        return python310_available()
    if name == "python3-pip":
        return python310_pip_available()
    if name == "node":
        return node20_available()
    if name == "npm":
        return npm_available()
    return shutil.which(name) is not None


def _path_text_absolute_non_root(text: str, *, allow_windows: bool = True) -> bool:
    if _service_path_entry_absolute_non_root(text, "posix"):
        return True
    return allow_windows and _service_path_entry_absolute_non_root(text, "windows")


def safe_service_home_path(value: object) -> str:
    text = str(value or DEFAULT_SERVICE_HOME).strip() or DEFAULT_SERVICE_HOME
    if any(char in text for char in "\r\n\x00"):
        raise ValueError("PULLWISE_SERVICE_HOME must be single-line")
    if not _path_text_absolute_non_root(text, allow_windows=os.name == "nt"):
        raise ValueError("PULLWISE_SERVICE_HOME must be an absolute non-root path")
    return text.rstrip("/\\") or text


def _service_path_entry_absolute_non_root(part: str, flavour: str) -> bool:
    if flavour == "windows":
        if part.count(":") > 1 or (":" in part and not _WINDOWS_DRIVE_RE.match(part)):
            return False
        path = PureWindowsPath(part)
        return path.is_absolute() and len(path.parts) > 1
    path = PurePosixPath(part)
    return path.is_absolute() and part != path.anchor


def _split_service_path(text: str) -> tuple[list[str], str, str]:
    if os.name == "nt":
        if ";" in text:
            return [part for part in text.split(";") if part], ";", "windows"
        if _service_path_entry_absolute_non_root(text, "windows"):
            return [text], ";", "windows"
        return [part for part in text.split(":") if part], ":", "posix"
    return [part for part in text.split(":") if part], ":", "posix"


def safe_service_path(value: object) -> str:
    text = str(value or DEFAULT_SERVICE_PATH).strip() or DEFAULT_SERVICE_PATH
    if any(char in text for char in "\r\n\x00"):
        raise ValueError("PULLWISE_SERVICE_PATH must be single-line")
    parts, separator, flavour = _split_service_path(text)
    if not parts:
        raise ValueError("PULLWISE_SERVICE_PATH must include at least one absolute path")
    if any(not _service_path_entry_absolute_non_root(part, flavour) for part in parts):
        raise ValueError("PULLWISE_SERVICE_PATH entries must be absolute non-root paths")
    return separator.join(dict.fromkeys(parts))

def dependency_packages(requirements: list[str]) -> list[str]:
    packages: list[str] = []
    for requirement in requirements:
        for package in _UBUNTU_2204_DEPENDENCY_PACKAGES.get(requirement, ()):
            if package not in packages:
                packages.append(package)
    return packages


def run_apt_command(command: list[str]) -> tuple[bool, str]:
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    completed = subprocess.run(command, env=env)
    if completed.returncode != 0:
        return False, f"{' '.join(command)} exited {completed.returncode}"
    return True, "ok"

def install_ubuntu_2204_dependencies(requirements: list[str], *, dry_run: bool = False) -> tuple[bool, str]:
    missing = [requirement for requirement in requirements if not dependency_available(requirement)]
    if not missing:
        return True, "dependencies present"
    node_missing = any(requirement in {"node", "npm"} for requirement in missing)
    package_requirements = [requirement for requirement in missing if requirement not in {"node", "npm"}]
    if node_missing:
        return (
            False,
            "missing Node.js 20+ and npm; install a trusted, pinned Node.js runtime before enabling the Codex provider",
        )
    packages = dependency_packages(package_requirements)
    if not packages:
        return False, f"missing dependencies without package mapping: {', '.join(missing)}"
    if not ubuntu_2204_host():
        return False, f"missing dependencies on unsupported host: {', '.join(missing)}"
    if not auto_install_dependencies_enabled():
        return False, f"missing dependencies and auto-install disabled: {', '.join(missing)}"
    if dry_run:
        print("apt-get update")
        print("DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends " + " ".join(packages))
        return True, f"would install Ubuntu 22.04 packages: {', '.join(packages)}"
    if os.geteuid() != 0:
        return False, f"missing dependencies require root to install on Ubuntu 22.04: {', '.join(missing)}"
    if shutil.which("apt-get") is None:
        return False, "apt-get not found on Ubuntu 22.04 host"
    for command in (
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "--no-install-recommends", *packages],
    ):
        ok, detail = run_apt_command(command)
        if not ok:
            return False, detail
    still_missing = [requirement for requirement in requirements if not dependency_available(requirement)]
    if still_missing:
        return False, f"dependencies still missing after install: {', '.join(still_missing)}"
    return True, f"installed Ubuntu 22.04 packages: {', '.join(packages)}"


def service_user_command(config: WorkerConfig | None, command: list[str]) -> str:
    service_user = str(getattr(config, "service_user", None) or DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
    service_home = safe_service_home_path(getattr(config, "service_home", None))
    path = provider_tool_path(config)
    quoted_command = " ".join(shlex.quote(str(part)) for part in command if str(part))
    shell_command = f'cd "$HOME" && exec {quoted_command}'
    return (
        f"sudo -u {shlex.quote(service_user)} env "
        f"HOME={shlex.quote(service_home)} "
        f"USERPROFILE={shlex.quote(service_home)} "
        f"CODEX_HOME={shlex.quote(provider_home_path(service_home, '.codex'))} "
        f"CODEX_SQLITE_HOME={shlex.quote(provider_home_path(service_home, '.codex-sqlite'))} "
        f"XDG_CONFIG_HOME={shlex.quote(provider_home_path(service_home, '.config'))} "
        f"XDG_CACHE_HOME={shlex.quote(provider_home_path(service_home, '.cache'))} "
        f"XDG_DATA_HOME={shlex.quote(provider_home_path(service_home, '.local', 'share'))} "
        f"PATH={shlex.quote(path)} "
        f"sh -lc {shlex.quote(shell_command)}"
    )


def provider_tool_path(config: WorkerConfig | None) -> str:
    service_home = safe_service_home_path(getattr(config, "service_home", None))
    service_path = safe_service_path(getattr(config, "service_path", None))
    path_parts = [
        f"{service_home}/.local/bin",
        f"{service_home}/.codex/bin",
        service_path,
    ]
    return os.pathsep.join(dict.fromkeys(part for part in path_parts if part))


def provider_home_path(service_home: str, *parts: str) -> str:
    home = safe_service_home_path(service_home)
    clean_parts = [part.strip("/\\") for part in parts if part]
    if _service_path_entry_absolute_non_root(home, "windows"):
        path = PureWindowsPath(home)
        for part in clean_parts:
            path /= part
        return str(path)
    return "/".join([home.rstrip("/"), *clean_parts])


def provider_process_env(config: WorkerConfig) -> dict[str, str]:
    service_home = safe_service_home_path(config.service_home)
    codex_sqlite_home = provider_home_path(service_home, ".codex-sqlite")
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
            "CODEX_SQLITE_HOME": codex_sqlite_home,
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
    home = safe_service_home_path(service_home)
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
    return value.strip().lower() not in _ENV_FALSE_VALUES


def env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = max(minimum, int(os.environ.get(name) or default))
    except (TypeError, ValueError, OverflowError):
        value = max(minimum, int(default))
    if maximum is not None:
        value = min(maximum, value)
    return value


def safe_worker_id(worker_id: object) -> str:
    safe_id = str(worker_id or "").strip()
    if not safe_id or len(safe_id) > _MAX_JOB_ID_LENGTH or not _SAFE_JOB_ID_RE.match(safe_id):
        raise ValueError("PULLWISE_WORKER_ID must be 1-128 characters of letters, numbers, dot, underscore, or dash")
    return safe_id


def safe_worker_service_name(service_name: object) -> str:
    safe_service_name = str(service_name or "").strip()
    if (
        not safe_service_name.startswith(DEFAULT_SERVICE_NAME)
        or ".." in safe_service_name
        or not _SAFE_WORKER_SERVICE_NAME_RE.match(safe_service_name)
    ):
        raise ValueError(f"refusing to use unexpected worker service name: {service_name}")
    return safe_service_name


def env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError, OverflowError):
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
        self.worker_id = safe_worker_id(
            getattr(args, "worker_id", None) or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        )
        configured_provider = str(getattr(args, "provider", None) or os.environ.get("PULLWISE_PROVIDER") or "").strip().lower()
        self.provider_chain = parse_provider_chain(
            os.environ.get("PULLWISE_PROVIDER_CHAIN"),
            configured_provider,
        )
        self.provider = configured_provider or (self.provider_chain[0] if self.provider_chain else "")
        self.poll_seconds = max(1, int(getattr(args, "poll_seconds", None) or env_int("PULLWISE_WORKER_POLL_SECONDS", 5)))
        self.poll_jitter_seconds = env_float("PULLWISE_WORKER_POLL_JITTER_SECONDS", 2, minimum=0.0)
        self.max_backoff_seconds = max(self.poll_seconds, env_int("PULLWISE_WORKER_MAX_BACKOFF_SECONDS", 60))
        checkout_root = getattr(args, "checkout_root", None) or os.environ.get("PULLWISE_CHECKOUT_ROOT")
        work_dir = getattr(args, "work_dir", None) or os.environ.get("PULLWISE_WORKER_WORK_DIR")
        self.work_dir = Path(checkout_root) if checkout_root else Path(work_dir or tempfile.gettempdir()) / "pullwise-worker"
        log_dir = getattr(args, "log_dir", None) or os.environ.get("PULLWISE_LOG_DIR")
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir()) / "pullwise-worker-logs"
        self.service_user = os.environ.get("PULLWISE_SERVICE_USER", DEFAULT_SERVICE_USER).strip() or DEFAULT_SERVICE_USER
        self.service_home = safe_service_home_path(os.environ.get("PULLWISE_SERVICE_HOME", DEFAULT_SERVICE_HOME))
        self.service_path = safe_service_path(os.environ.get("PULLWISE_SERVICE_PATH", DEFAULT_SERVICE_PATH))
        self.service_name = safe_worker_service_name(
            os.environ.get("PULLWISE_SERVICE_NAME", DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
        )
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
        self.watcher_poll_seconds = env_int("PULLWISE_WATCHER_POLL_SECONDS", self.poll_seconds)
        self.watcher_service_name = safe_worker_service_name(
            os.environ.get("PULLWISE_WATCHER_SERVICE_NAME", f"{self.service_name}-watcher").strip()
            or f"{self.service_name}-watcher"
        )
        self.watcher_service_file = (
            os.environ.get("PULLWISE_WATCHER_SERVICE_FILE", f"/etc/systemd/system/{self.watcher_service_name}.service").strip()
            or f"/etc/systemd/system/{self.watcher_service_name}.service"
        )
        self.remote_uninstall_finalizer = env_bool("PULLWISE_REMOTE_UNINSTALL_FINALIZER", True)
        self.uninstall_marker_file = (
            os.environ.get("PULLWISE_UNINSTALL_MARKER_FILE", f"/run/{self.service_name}/uninstall-requested").strip()
            or f"/run/{self.service_name}/uninstall-requested"
        )
        default_codex_command = default_provider_command(self.service_home, "codex")
        self.codex_command = getattr(args, "codex_command", None) or os.environ.get("PULLWISE_CODEX_COMMAND") or default_codex_command
        self.codex_doctor_timeout_seconds = env_int("PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS", 60, minimum=10)
        readiness_check_seconds_fallback = env_int(
            "PULLWISE_READINESS_CHECK_SECONDS",
            DEFAULT_ACTIVE_READINESS_CHECK_SECONDS,
            minimum=10,
        )
        self.active_readiness_check_seconds = env_int(
            "PULLWISE_ACTIVE_READINESS_CHECK_SECONDS",
            readiness_check_seconds_fallback,
            minimum=10,
        )
        self.degraded_readiness_check_seconds = env_int(
            "PULLWISE_DEGRADED_READINESS_CHECK_SECONDS",
            DEFAULT_DEGRADED_READINESS_CHECK_SECONDS,
            minimum=10,
        )
        self.readiness_check_seconds = self.active_readiness_check_seconds
        self.codex_quota_check_seconds = env_int(
            "PULLWISE_CODEX_QUOTA_CHECK_SECONDS",
            self.active_readiness_check_seconds,
            minimum=10,
        )
        self.codex_quota_degraded_check_seconds = env_int(
            "PULLWISE_CODEX_QUOTA_DEGRADED_CHECK_SECONDS",
            self.degraded_readiness_check_seconds,
            minimum=10,
        )
        self.codex_quota_min_remaining_percent = env_float(
            "PULLWISE_CODEX_QUOTA_MIN_REMAINING_PERCENT",
            5.0,
            minimum=0.0,
        )
        self.machine_metrics_interval_seconds = env_int(
            "PULLWISE_WORKER_MACHINE_METRICS_SECONDS",
            DEFAULT_MACHINE_METRICS_INTERVAL_SECONDS,
            minimum=1,
        )
        self.result_upload_attempts = env_int("PULLWISE_RESULT_UPLOAD_ATTEMPTS", 5, maximum=20)
        self.result_upload_compress_min_bytes = env_int("PULLWISE_RESULT_UPLOAD_COMPRESS_MIN_BYTES", 1024, minimum=0)
        self.result_upload_pending_backoff_base_seconds = env_int(
            "PULLWISE_RESULT_UPLOAD_PENDING_BACKOFF_BASE_SECONDS",
            30,
            minimum=1,
        )
        self.result_upload_pending_backoff_max_seconds = env_int(
            "PULLWISE_RESULT_UPLOAD_PENDING_BACKOFF_MAX_SECONDS",
            15 * 60,
            minimum=1,
        )
        self.result_upload_pending_max_age_seconds = env_int(
            "PULLWISE_RESULT_UPLOAD_PENDING_MAX_AGE_SECONDS",
            7 * 24 * 60 * 60,
            minimum=60,
        )
        self.result_upload_pending_max_attempts = env_int(
            "PULLWISE_RESULT_UPLOAD_PENDING_MAX_ATTEMPTS",
            100,
            minimum=1,
            maximum=10000,
        )
        self.failed_checkout_retention_seconds = env_int("PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS", 0, minimum=0)
        self.max_checkout_bytes = env_int(
            "PULLWISE_MAX_CHECKOUT_BYTES",
            20 * 1024 * 1024 * 1024,
            maximum=100 * 1024 * 1024 * 1024,
        )
        self.repo_cache_max_bytes = env_int(
            "PULLWISE_REPO_CACHE_MAX_BYTES",
            max(1, self.max_checkout_bytes // 2),
            minimum=0,
            maximum=100 * 1024 * 1024 * 1024,
        )
        self.repo_cache_ttl_seconds = env_int(
            "PULLWISE_REPO_CACHE_TTL_SECONDS",
            14 * 24 * 60 * 60,
            minimum=0,
        )
        self.max_repo_files = _DEFAULT_MAX_REPO_FILES
        self.max_repo_bytes = _DEFAULT_MAX_REPO_BYTES
        self.cleanup_interval_seconds = env_int("PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS", 3600, minimum=60)
        self.log_retention_seconds = env_int("PULLWISE_LOG_RETENTION_SECONDS", 14 * 24 * 60 * 60, minimum=0)
        self.max_log_bytes = env_int("PULLWISE_MAX_LOG_BYTES", 1024 * 1024 * 1024, maximum=10 * 1024 * 1024 * 1024)
        self.scan_summary_log_max_bytes = env_int(
            "PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES",
            10 * 1024 * 1024,
            minimum=1024,
            maximum=100 * 1024 * 1024,
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
        if not isinstance(parsed, dict):
            raise PullwiseRequestError("JSON response must be an object")
        return parsed


def read_pullwise_response_body(response: object) -> bytes:
    try:
        body = response.read(WORKER_HTTP_RESPONSE_MAX_BYTES + 1)
    except TypeError as exc:
        raise PullwiseRequestError("response body reader must support bounded reads") from exc
    if len(body) > WORKER_HTTP_RESPONSE_MAX_BYTES:
        raise PullwiseRequestError("response body too large")
    return body


def clean_protocol_text(value: object, max_length: int = 500) -> str:
    try:
        limit = max(0, int(max_length))
    except (TypeError, ValueError):
        limit = 500
    text = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    return text[:limit]


def redact_secrets(text: object, config: WorkerConfig | None = None) -> str:
    redacted = str(text or "")
    token = str(getattr(config, "worker_token", "") or "") if config else ""
    if token:
        redacted = redacted.replace(token, "[redacted]")
    redacted = re.sub(
        r"(?i)\b(authorization\s*:\s*)(bearer|basic)\s+([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)} [redacted]",
        redacted,
    )
    redacted = re.sub(r"(?i)x-access-token:[^@\s]+@", "x-access-token:[redacted]@", redacted)
    redacted = re.sub(r"\b(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[redacted]", redacted)
    return redacted


def client_protocol_text(value: object, config: WorkerConfig | None = None, max_length: int = 500) -> str:
    return clean_protocol_text(redact_secrets(value, config), max_length)


def client_active_job_ids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    job_ids: list[str] = []
    for value in values:
        job_id = str(value or "").strip()
        if len(job_id) > _MAX_JOB_ID_LENGTH:
            continue
        if not job_id or job_id in {".", ".."} or not _SAFE_JOB_ID_RE.match(job_id):
            continue
        if job_id not in job_ids:
            job_ids.append(job_id)
    return job_ids


def client_ready_providers(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    providers: list[str] = []
    for value in values:
        provider = str(value or "").strip().lower()
        if provider not in SUPPORTED_REVIEW_PROVIDERS or provider in providers:
            continue
        providers.append(provider)
    return providers


def worker_registration_payload(config: WorkerConfig) -> dict:
    worker_id = str(config.worker_id)
    service_home = Path(str(getattr(config, "service_home", "") or DEFAULT_SERVICE_HOME))
    configured_root = os.environ.get("PULLWISE_WORKER_ROOT", "").strip()
    worker_root = Path(configured_root) if configured_root else service_home / "workers" / worker_id
    codex_transport = ["stdio", "unix"] if os.name == "posix" else ["stdio"]
    return {
        "protocol_version": WORKER_REVIEW_PROTOCOL_VERSION,
        "worker": {
            "worker_id": worker_id,
            "worker_group": "default",
            "worker_version": __version__,
            "hostname": socket.gethostname(),
            "concurrency": {
                "max_active_jobs": 1,
                "maintains_local_queue": False,
                "prefetch_jobs": False,
            },
            "isolation": {
                "isolated_codex_home": True,
                "isolated_codex_sqlite_home": True,
                "isolated_app_server": True,
                "isolated_workspace": True,
                "isolated_auth": True,
                "codex_home": str(worker_root / "codex-home"),
                "workspace_root": str(worker_root / "workspaces"),
            },
            "platform": {
                "os": "linux" if sys.platform.startswith("linux") else sys.platform,
                "arch": platform.machine() or "unknown",
            },
            "capabilities": {
                "codex_app_server": True,
                "codex_app_server_transport": codex_transport,
                "full_repo_scan": True,
                "logical_subagents": True,
                "physical_parallel_subagents": False,
                "artifact_upload": True,
                "progress_events": True,
                "cancellation": True,
                "intent_test_validation": True,
                "disposable_validation_workspace": True,
                "max_active_jobs": 1,
            },
        },
    }


class PullwiseClient:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.headers = {
            "Authorization": f"Bearer {config.worker_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, path: str, payload: dict, *, compress: bool = False) -> PullwiseResponse:
        body = json.dumps(payload).encode("utf-8")
        headers = dict(self.headers)
        if compress and len(body) >= self.config.result_upload_compress_min_bytes:
            uncompressed_length = len(body)
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
            headers["X-Pullwise-Uncompressed-Length"] = str(uncompressed_length)
        request = urllib.request.Request(
            f"{self.config.server_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=WORKER_HTTP_TIMEOUT_SECONDS) as response:
                return PullwiseResponse(read_pullwise_response_body(response))
        except urllib.error.HTTPError as exc:
            raise PullwiseHTTPError(http_error_message(exc, self.config), exc.code) from exc
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
                return PullwiseResponse(read_pullwise_response_body(response))
        except urllib.error.HTTPError as exc:
            raise PullwiseHTTPError(http_error_message(exc, self.config), exc.code) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise PullwiseRequestError(str(exc)) from exc

    def register(self) -> dict:
        response = self.post("/v1/workers/register", worker_registration_payload(self.config))
        return response.json()

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
        codex_quota: dict | None = None,
        progress: dict | None = None,
        worker_state: str | None = None,
        active_thread_id: str | None = None,
    ) -> dict:
        reported_running_jobs = 1 if int(running_jobs or 0) > 0 else 0
        active_run_id = ""
        if isinstance(progress, dict):
            active_run_id = str(progress.get("run_id") or progress.get("runId") or "").strip()
        codex_server_status = "ready" if codex_ready is not False else "needs_attention"
        worker_state_status = client_protocol_text(worker_state, self.config, 80)
        thread_id = client_protocol_text(active_thread_id, self.config, 160) or None
        status = "idle"
        if reported_running_jobs:
            status = worker_state_status if worker_state_status in {"cancelling", "finishing", "failure_handling"} else "busy"
        payload = {
            "protocol_version": WORKER_REVIEW_PROTOCOL_VERSION,
            "worker_id": self.config.worker_id,
            "status": status,
            "active_run_id": active_run_id or None,
            "hostname": socket.gethostname(),
            "concurrency": {
                "max_active_jobs": 1,
                "active_jobs": reported_running_jobs,
                "available_job_slots": 0 if reported_running_jobs else 1,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
            "codex_app_server": {
                "status": codex_server_status,
                "transport": "stdio",
                "active_thread_id": thread_id,
            },
        }
        if last_error:
            payload["last_error"] = client_protocol_text(last_error, self.config, 500)
        if doctor_status:
            payload["doctor_status"] = client_protocol_text(doctor_status, self.config, 80)
        if codex_ready is not None:
            payload["codex_ready"] = codex_ready
        if ready_providers is not None:
            payload["ready_providers"] = client_ready_providers(ready_providers)
        if systemd_active is not None:
            payload["systemd_active"] = systemd_active
        if doctor_checked_at is not None:
            payload["doctor_checked_at"] = doctor_checked_at
        if isinstance(machine_metrics, dict):
            payload["machine_metrics"] = machine_metrics
        if isinstance(codex_quota, dict):
            payload["codex_quota"] = codex_quota
        if isinstance(progress, dict):
            payload["progress"] = progress
        response = self.post(f"/v1/workers/{url_path_segment(self.config.worker_id)}/heartbeat", payload)
        return response.json()

    def agent_configs(self) -> dict:
        response = self.post(f"/v1/workers/{url_path_segment(self.config.worker_id)}/agent-configs", {"worker_id": self.config.worker_id})
        return response.json()

    def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
        payload = {"worker_id": self.config.worker_id, "status": client_protocol_text(status, self.config, 80)}
        if error:
            payload["error"] = client_protocol_text(error, self.config, 500)
        self.post(f"/worker/commands/{url_path_segment(command_id)}/status", payload)

    def command_poll(self) -> dict:
        response = self.post("/worker/commands/poll", {"worker_id": self.config.worker_id})
        return response.json()

    def log_stream_lines(self, session_id: str, lines: list[dict]) -> dict:
        response = self.post(
            f"/worker/log-streams/{url_path_segment(session_id)}/lines",
            {"worker_id": self.config.worker_id, "lines": lines},
        )
        return response.json()

    def claim(self) -> dict | None:
        response = self.post(
            f"/v1/workers/{url_path_segment(self.config.worker_id)}/lease",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker_id": self.config.worker_id,
                "capacity": {
                    "available_job_slots": 1,
                    "active_jobs": 0,
                    "maintains_local_queue": False,
                    "local_queue_depth": 0,
                },
                "capabilities": {
                    "full_repo_scan": True,
                    "codex_app_server": True,
                    "isolated_codex_home": True,
                    "progress_events": True,
                    "cancellation": True,
                    "intent_test_validation": True,
                },
            },
        )
        parsed = response.json()
        job = parsed.get("job")
        if job is None:
            return None
        if not isinstance(job, dict):
            raise PullwiseRequestError("claim response job must be an object")
        return job

    def event(self, run_id: str, payload: dict) -> dict:
        response = self.post(f"/v1/review-runs/{url_path_segment(run_id)}/events", payload)
        return response.json()

    def progress(
        self,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
        *,
        log_time: int | None = None,
    ) -> None:
        try:
            safe_log_time = int(log_time if log_time is not None else time.time())
        except (TypeError, ValueError):
            safe_log_time = int(time.time())
        payload = {
            "phase": client_protocol_text(phase, self.config, 80),
            "progress": progress,
            "message": client_protocol_text(message, self.config, 500),
            "started_at": safe_log_time,
            "log_time": safe_log_time,
            "logs_summary": client_protocol_text(logs_summary, self.config, 1000),
        }
        self.post(f"/worker/jobs/{url_path_segment(job_id)}/progress", payload)

    def artifact(self, job_id: str, artifact_id: str, payload: dict) -> dict:
        run_id = str(payload.get("run_id") or payload.get("runId") or f"run_{job_id}").strip()
        response = self.post(f"/v1/review-runs/{url_path_segment(run_id)}/artifacts", payload, compress=True)
        return response.json()

    def result(self, job_id: str, payload: dict) -> None:
        envelope = payload.get("reviewWorkerProtocol") if isinstance(payload.get("reviewWorkerProtocol"), dict) else {}
        envelope_job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
        run_id = str(envelope_job.get("run_id") or payload.get("run_id") or f"run_{job_id}").strip()
        self.post(f"/v1/review-runs/{url_path_segment(run_id)}/result", payload, compress=True)


def url_path_segment(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise PullwiseRequestError("URL path segment is required")
    if len(text) > 128 or any(char in text for char in "\r\n\x00"):
        raise PullwiseRequestError("URL path segment is invalid")
    return urllib.parse.quote(text, safe="")


def http_error_message(exc: urllib.error.HTTPError, config: WorkerConfig | None = None) -> str:
    reason = getattr(exc, "reason", None) or getattr(exc, "msg", "") or "error"
    detail = ""
    try:
        body = exc.read(8192)
    except Exception:
        body = b""
    if body:
        text = body.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            detail = text
        else:
            if isinstance(parsed, dict):
                for key in ("error", "message", "detail"):
                    value = parsed.get(key)
                    if isinstance(value, str) and value.strip():
                        detail = value.strip()
                        break
            elif isinstance(parsed, str):
                detail = parsed.strip()
        detail = clean_protocol_text(detail)[:1000]
    message = f"HTTP {exc.code}: {reason}"
    if detail:
        message = f"{message}: {detail}"
    return redact_secrets(message, config)


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
            "logs",
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
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--work-dir")
    parser.add_argument("--checkout-root")
    parser.add_argument("--log-dir")
    parser.add_argument("--provider")
    parser.add_argument("--codex-command")
    parser.add_argument("--lines", type=int, default=120)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove-config", action="store_true")
    parser.add_argument("--remove-logs", action="store_true")
    args = parser.parse_args()

    if args.command in {"start", "stop", "status", "restart", "logs"}:
        try:
            config = WorkerConfig(args, require_worker_token=False, validate_server_url=False)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        if args.command == "logs":
            raise SystemExit(worker_logs(config, lines=args.lines, follow=args.follow, dry_run=args.dry_run))
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
    from .review_worker_v1 import ReviewWorkerV1

    worker = ReviewWorkerV1(config, client=PullwiseClient(config))
    worker.run(once=args.once)

__all__ = [name for name in globals() if name == "__version__" or not (name.startswith("__") and name.endswith("__"))]
