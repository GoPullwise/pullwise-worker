"""Fixed strict-v1 executable probes with bounded process-tree cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Mapping

try:
    from scripts.agent_first_contract_process import (
        _windows_kill_process_tree,
        run_bounded_process,
    )
except ModuleNotFoundError:
    from agent_first_contract_process import (  # type: ignore[no-redef]
        _windows_kill_process_tree,
        run_bounded_process,
    )


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
    "server.progress-eta-fixtures": {
        "repo": "server",
        "runner": "python_unittest",
        "nodes": (
            "tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_progress_records_worker_reported_phase_steps_message_and_log_summary",
            "tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_eta_persists_and_batch_status_exposes_arbitrary_concurrency",
            "tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_eta_rejects_invalid_numbers_ranges_and_terminal_payloads",
            "tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_heartbeat_eta_is_persisted_and_terminal_result_clears_scan_eta",
            "tests.test_worker_pull_routes.WorkerPullRoutesTest.test_delayed_lower_sequence_event_cannot_overwrite_newer_scan_progress",
        ),
        "timeout_seconds": 300,
        "minimum_tests": 5,
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
    "web.progress-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/components/scan-progress.test.jsx",),
        "timeout_seconds": 300,
        "minimum_tests": 1,
        "allowed_skips": 0,
    },
    "web.timing-fixtures": {
        "repo": "web",
        "runner": "node_vitest",
        "nodes": ("src/components/scan-timing.test.jsx",),
        "timeout_seconds": 300,
        "minimum_tests": 6,
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


def _execute_probe(
    runner_id: str,
    repo_root: Path,
    *,
    spec: Mapping[str, Any],
    argv: list[str],
    scratch_root: Path,
) -> dict[str, Any]:
    process = run_bounded_process(
        argv,
        cwd=repo_root,
        scratch_root=scratch_root,
        timeout_seconds=int(spec["timeout_seconds"]),
        max_output_bytes=int(spec.get("max_output_bytes", 8 * 1024 * 1024)),
    )
    if process["status"] == "start_failed":
        return _indeterminate_result(runner_id, "process_start_failed")
    if process["status"] in {"timeout", "cleanup_unconfirmed"}:
        result = _indeterminate_result(
            runner_id,
            "probe_timeout"
            if process["status"] == "timeout"
            else "process_tree_cleanup_unconfirmed",
        )
        result["output_sha256"] = process["output_sha256"]
        return result
    output = process["output"]
    output_sha256 = process["output_sha256"]
    if process["output_too_large"]:
        result = _indeterminate_result(runner_id, "output_too_large")
        result.update(returncode=process["returncode"], output_sha256=output_sha256)
        return result
    observed, skipped = _parse_counts(spec, output)
    if observed is None or skipped is None:
        result = _indeterminate_result(runner_id, "test_count_unparseable")
        result.update(returncode=process["returncode"], output_sha256=output_sha256)
        return result
    if observed < int(spec["minimum_tests"]):
        result = _indeterminate_result(runner_id, "insufficient_tests")
        result.update(
            returncode=process["returncode"],
            output_sha256=output_sha256,
            observed_tests=observed,
            observed_skips=skipped,
        )
        return result
    if skipped > int(spec.get("allowed_skips", 0)):
        result = _indeterminate_result(runner_id, "unexpected_skips")
        result.update(
            returncode=process["returncode"],
            output_sha256=output_sha256,
            observed_tests=observed,
            observed_skips=skipped,
        )
        return result
    if process["returncode"] != 0:
        return {
            "id": runner_id,
            "runner_id": runner_id,
            "status": "failed",
            "returncode": process["returncode"],
            "output_sha256": output_sha256,
            "observed_tests": observed,
            "observed_skips": skipped,
        }
    return {
        "id": runner_id,
        "runner_id": runner_id,
        "status": "passed",
        "returncode": 0,
        "output_sha256": output_sha256,
        "observed_tests": observed,
        "observed_skips": skipped,
    }


def _python_module_for_node(repo_root: Path, node: str) -> str | None:
    parts = node.split(".")
    for length in range(len(parts), 1, -1):
        path = repo_root.joinpath(*parts[:length]).with_suffix(".py")
        if path.is_file() and not path.is_symlink():
            return ".".join(parts[:length])
    return None


def _fixed_entries_available(spec: Mapping[str, Any], repo_root: Path) -> bool:
    if spec["runner"] == "node_vitest":
        vitest = repo_root / "node_modules" / "vitest" / "vitest.mjs"
        return vitest.is_file() and not vitest.is_symlink() and all(
            (repo_root / node).is_file() and not (repo_root / node).is_symlink()
            for node in spec["nodes"]
        )
    return all(_python_module_for_node(repo_root, node) for node in spec["nodes"])


def _python_imports_available(
    spec: Mapping[str, Any], repo_root: Path, scratch_root: Path
) -> bool:
    modules = sorted(
        {
            module
            for node in spec["nodes"]
            if (module := _python_module_for_node(repo_root, node)) is not None
        }
    )
    for module in modules:
        result = run_bounded_process(
            [
                sys.executable,
                "-B",
                "-c",
                f"import importlib; importlib.import_module({module!r})",
            ],
            cwd=repo_root,
            scratch_root=scratch_root,
            timeout_seconds=30,
            max_output_bytes=1024 * 1024,
        )
        if result["status"] != "completed" or result["returncode"] != 0:
            return False
    return True


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
    if not _fixed_entries_available(spec, repo_root):
        return _indeterminate_result(runner_id, "fixed_entry_unavailable")
    executable = sys.executable if spec["runner"] == "python_unittest" else node
    assert executable is not None
    argv = build_test_argv(
        runner_id,
        runner_catalog=runner_catalog,
        python_executable=sys.executable,
        npm_executable=executable,
    )
    with tempfile.TemporaryDirectory(prefix="pullwise-contract-probe-") as scratch:
        scratch_root = Path(scratch)
        if spec["runner"] == "python_unittest" and not _python_imports_available(
            spec, repo_root, scratch_root
        ):
            return _indeterminate_result(runner_id, "fixed_entry_unavailable")
        return _execute_probe(
            runner_id,
            repo_root,
            spec=spec,
            argv=argv,
            scratch_root=scratch_root,
        )
