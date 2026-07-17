"""Fixed strict-v1 executable probes with bounded process-tree cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from typing import Any, Mapping


RUNNER_CATALOG: dict[str, dict[str, Any]] = {
    "server.cancellation-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": ("tests.test_cancellation_handshake",),
        "timeout_seconds": 300,
        "minimum_tests": 17,
        "allowed_skips": 0,
    },
    "server.policy-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": (
            "tests.test_worker_admin_routes.WorkerAdminRoutesTest.test_admin_plan_agent_config_keeps_only_canonical_review_worker_policy",
        ),
        "timeout_seconds": 300,
        "minimum_tests": 1,
        "allowed_skips": 0,
    },
    "server.result-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": ("tests.test_review_worker_protocol_v1",),
        "timeout_seconds": 300,
        "minimum_tests": 28,
        "allowed_skips": 0,
    },
    "server.route-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": ("tests.test_worker_pull_routes",),
        "timeout_seconds": 300,
        "minimum_tests": 123,
        "allowed_skips": 0,
    },
    "server.system-limit-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": (
            "tests.test_configuration_contracts.ConfigurationContractsTest.test_review_phase_limits_are_global_admin_config",
        ),
        "timeout_seconds": 300,
        "minimum_tests": 1,
        "allowed_skips": 0,
    },
    "web.api-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/api/pullwise.test.js",),
        "timeout_seconds": 300,
        "minimum_tests": 8,
        "allowed_skips": 0,
    },
    "web.flow-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/screens/flow.test.jsx",),
        "timeout_seconds": 300,
        "minimum_tests": 75,
        "allowed_skips": 0,
    },
    "web.history-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/screens/issues.test.jsx",),
        "timeout_seconds": 300,
        "minimum_tests": 69,
        "allowed_skips": 0,
    },
    "web.normalizer-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/lib/pullwise-data.test.js",),
        "timeout_seconds": 300,
        "minimum_tests": 75,
        "allowed_skips": 0,
    },
}
RunnerCatalog = Mapping[str, Mapping[str, Any]]
UNITTEST_COUNT = re.compile(rb"Ran ([0-9]+) tests? in")
UNITTEST_SKIPS = re.compile(rb"skipped=([0-9]+)")


def build_test_argv(
    runner_id: str,
    *,
    runner_catalog: RunnerCatalog,
    python_executable: str,
    npm_executable: str,
) -> list[str]:
    spec = runner_catalog[runner_id]
    if spec["runner"] == "python_unittest":
        return [python_executable, "-B", "-m", "unittest", *spec["nodes"]]
    if spec["runner"] == "node_vitest":
        return [
            npm_executable,
            "node_modules/vitest/vitest.mjs",
            "run",
            "--reporter=json",
            *spec["nodes"],
        ]
    raise ValueError("unsupported_fixed_runner")


def _probe_environment() -> dict[str, str]:
    allowed = {
        "CI",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["CI"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def _kill_process_tree(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        if not taskkill.is_file():
            return False
        result = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            shell=False,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0 and process.poll() is not None
    try:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _parse_counts(spec: Mapping[str, Any], output: bytes) -> tuple[int | None, int | None]:
    if spec["runner"] == "python_unittest":
        count_match = UNITTEST_COUNT.search(output)
        if count_match is None:
            return None, None
        skip_match = UNITTEST_SKIPS.search(output)
        return int(count_match.group(1)), int(skip_match.group(1)) if skip_match else 0
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    observed = payload.get("numTotalTests")
    pending = payload.get("numPendingTests", 0)
    todo = payload.get("numTodoTests", 0)
    if isinstance(observed, bool) or not isinstance(observed, int):
        return None, None
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (pending, todo)):
        return None, None
    return observed, pending + todo


def _indeterminate_result(runner_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": runner_id,
        "runner_id": runner_id,
        "status": "indeterminate",
        "reason": reason,
        "returncode": None,
        "output_sha256": None,
        "observed_tests": None,
        "observed_skips": None,
    }


def run_probe(
    runner_id: str,
    repo_root: Path,
    *,
    runner_catalog: RunnerCatalog,
) -> dict[str, Any]:
    spec = runner_catalog[runner_id]
    node = shutil.which("node")
    if spec["runner"] == "node_vitest" and not node:
        return _indeterminate_result(runner_id, "tool_unavailable")
    executable = sys.executable if spec["runner"] == "python_unittest" else node
    assert executable is not None
    argv = build_test_argv(
        runner_id,
        runner_catalog=runner_catalog,
        python_executable=sys.executable,
        npm_executable=executable,
    )
    popen_options: dict[str, Any] = {
        "cwd": repo_root,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": _probe_environment(),
        "shell": False,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **popen_options)
    except OSError:
        return _indeterminate_result(runner_id, "process_start_failed")
    try:
        output, _ = process.communicate(timeout=int(spec["timeout_seconds"]))
    except subprocess.TimeoutExpired:
        cleanup_confirmed = _kill_process_tree(process)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            output = b""
            cleanup_confirmed = False
        result = _indeterminate_result(
            runner_id,
            "probe_timeout" if cleanup_confirmed else "process_tree_cleanup_unconfirmed",
        )
        result["output_sha256"] = hashlib.sha256(output).hexdigest()
        return result

    output_sha256 = hashlib.sha256(output).hexdigest()
    observed, skipped = _parse_counts(spec, output)
    if process.returncode != 0:
        return {
            "id": runner_id,
            "runner_id": runner_id,
            "status": "failed",
            "returncode": process.returncode,
            "output_sha256": output_sha256,
            "observed_tests": observed,
            "observed_skips": skipped,
        }
    if observed is None or skipped is None:
        result = _indeterminate_result(runner_id, "test_count_unparseable")
        result.update(returncode=0, output_sha256=output_sha256)
        return result
    if observed < int(spec["minimum_tests"]):
        result = _indeterminate_result(runner_id, "insufficient_tests")
        result.update(
            returncode=0,
            output_sha256=output_sha256,
            observed_tests=observed,
            observed_skips=skipped,
        )
        return result
    if skipped > int(spec.get("allowed_skips", 0)):
        result = _indeterminate_result(runner_id, "unexpected_skips")
        result.update(
            returncode=0,
            output_sha256=output_sha256,
            observed_tests=observed,
            observed_skips=skipped,
        )
        return result
    return {
        "id": runner_id,
        "runner_id": runner_id,
        "status": "passed",
        "returncode": 0,
        "output_sha256": output_sha256,
        "observed_tests": observed,
        "observed_skips": skipped,
    }
