"""Fixed strict-v1 executable probes with bounded process-tree cleanup."""

from __future__ import annotations

import hashlib
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


try:
    from scripts.agent_first_contract_runner_catalog import RUNNER_CATALOG
except ModuleNotFoundError:
    from agent_first_contract_runner_catalog import RUNNER_CATALOG  # type: ignore[no-redef]
RunnerCatalog = Mapping[str, Mapping[str, Any]]
UNITTEST_COUNT = re.compile(rb"Ran ([0-9]+) tests? in")
UNITTEST_SKIPS = re.compile(rb"skipped=([0-9]+)")


def runner_catalog_sha256(runner_catalog: RunnerCatalog) -> str:
    payload = json.dumps(
        runner_catalog,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    if spec["runner"] == "python_script":
        return [python_executable, "-B", *spec["nodes"]]
    if spec["runner"] == "node_vitest":
        return [
            npm_executable,
            "node_modules/vitest/vitest.mjs",
            "run",
            "--reporter=json",
            *spec["nodes"],
        ]
    if spec["runner"] == "node_script":
        return [npm_executable, *spec["nodes"]]
    raise ValueError("unsupported_fixed_runner")


def _parse_counts(spec: Mapping[str, Any], output: bytes) -> tuple[int | None, int | None]:
    if spec["runner"] in {"python_script", "python_unittest"}:
        count_matches = UNITTEST_COUNT.findall(output)
        if not count_matches:
            return None, None
        skip_matches = UNITTEST_SKIPS.findall(output)
        return int(count_matches[-1]), int(skip_matches[-1]) if skip_matches else 0
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


def _node_report_declares_failure(spec: Mapping[str, Any], output: bytes) -> bool:
    if spec["runner"] not in {"node_script", "node_vitest"}:
        return False
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return True
    for field in ("numFailedTests", "numFailedTestSuites"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return True
    return False


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
    output_sha256 = process["output_sha256"]
    if process["output_too_large"]:
        result = _indeterminate_result(runner_id, "output_too_large")
        result.update(returncode=process["returncode"], output_sha256=output_sha256)
        return result
    if process["status"] in {"timeout", "cleanup_unconfirmed"}:
        result = _indeterminate_result(
            runner_id,
            "probe_timeout"
            if process["status"] == "timeout"
            else "process_tree_cleanup_unconfirmed",
        )
        result["output_sha256"] = output_sha256
        return result
    output = process["output"]
    observed, skipped = _parse_counts(spec, output)
    if process["returncode"] != 0:
        if observed == 0 and skipped == 0:
            result = _indeterminate_result(runner_id, "insufficient_tests")
            result.update(
                returncode=process["returncode"],
                output_sha256=output_sha256,
                observed_tests=observed,
                observed_skips=skipped,
            )
            return result
        return {
            "id": runner_id,
            "runner_id": runner_id,
            "status": "failed",
            "returncode": process["returncode"],
            "output_sha256": output_sha256,
            "observed_tests": observed,
            "observed_skips": skipped,
        }
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
    if _node_report_declares_failure(spec, output):
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
    if spec["runner"] in {"node_script", "python_script"}:
        return all(
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
    if spec["runner"] in {"node_script", "node_vitest"} and not node:
        return _indeterminate_result(runner_id, "tool_unavailable")
    if not _fixed_entries_available(spec, repo_root):
        return _indeterminate_result(runner_id, "fixed_entry_unavailable")
    executable = (
        sys.executable
        if spec["runner"] in {"python_script", "python_unittest"}
        else node
    )
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
