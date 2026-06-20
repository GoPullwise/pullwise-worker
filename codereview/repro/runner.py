from __future__ import annotations

import concurrent.futures
import json
import shutil
from pathlib import Path

from ..codex_runner import run_codex_exec
from ..config import CodexConfig, ReviewConfig
from ..judge.validate import validate_repro_result
from ..utils.paths import safe_path_component
from ..utils.process import compact_process_output, run_process
from .filesystem_guard import guard_worker_result
from .worker_dir import create_worker_dir


def run_repro_workers_parallel(checkout: Path, run: Path, candidates: list[dict], config: ReviewConfig) -> list[dict]:
    if not config.repro.enabled:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.repro.max_workers) as executor:
        futures = [executor.submit(run_repro_worker, checkout, run, candidate, config) for candidate in candidates]
        return [future.result() for future in futures]


def run_repro_worker(checkout: Path, run: Path, candidate: dict, config: ReviewConfig) -> dict:
    issue_id = safe_path_component(candidate.get("issue_id") or candidate.get("candidate_id"), default="candidate")
    worker = run / "workers" / issue_id
    checkout_status_before = git_status_porcelain(
        checkout,
        ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/", ".codegraph/"),
    )
    create_worker_dir(checkout, worker, candidate)
    copy_slice_context(run, worker, candidate)
    prompt = (checkout / ".codereview" / "prompts" / "repro_worker.md").read_text(encoding="utf-8")
    output = worker / "result.json"
    process = run_codex_exec(
        cd=worker,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "repro_result.schema.json",
        output_file=output,
        sandbox="workspace-write",
        timeout_seconds=config.repro.timeout_seconds,
        config=config.codex,
        env=worker_env(worker, config.codex),
    )
    if process.returncode != 0:
        checkout_status_after = git_status_porcelain(
            checkout,
            ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/", ".codegraph/"),
        )
        violations: list[str] = []
        if checkout_status_after != checkout_status_before:
            violations.append("original checkout changed during repro worker execution")
        failure = process_failure_reason("repro codex exec", process)
        return {
            "candidate_id": issue_id,
            "worker": str(worker),
            "process": process.to_dict(),
            "result": blocked_repro_result(issue_id, failure),
            "status": "blocked",
            "blocked_reason": failure,
            "filesystem_violations": violations,
            "checkout_status_before": checkout_status_before,
            "checkout_status_after": checkout_status_after,
        }
    parsed = {}
    if output.is_file():
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            parsed = blocked_repro_result(issue_id, f"repro produced invalid JSON: {exc}")
    else:
        parsed = blocked_repro_result(issue_id, "repro did not produce an output file")
    violations = [*guard_worker_result(worker, parsed), *validate_repro_result(parsed, expected_candidate_id=issue_id)]
    checkout_status_after = git_status_porcelain(
        checkout,
        ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/", ".codegraph/"),
    )
    if checkout_status_after != checkout_status_before:
        violations.append("original checkout changed during repro worker execution")
    return {
        "candidate_id": issue_id,
        "worker": str(worker),
        "process": process.to_dict(),
        "result": parsed,
        "status": str(parsed.get("status") or "unknown") if isinstance(parsed, dict) else "unknown",
        "blocked_reason": str(parsed.get("why_not_reproduced") or parsed.get("summary") or "") if isinstance(parsed, dict) and parsed.get("status") == "blocked" else "",
        "filesystem_violations": violations,
        "checkout_status_before": checkout_status_before,
        "checkout_status_after": checkout_status_after,
    }


def worker_env(worker: Path, codex: CodexConfig | None = None) -> dict[str, str]:
    import os

    env = os.environ.copy()
    for child in ("home", "tmp", "cache", "cache/npm", "cache/pip", "cache/pycache"):
        (worker / child).mkdir(parents=True, exist_ok=True)
    shared_keys = set()
    if codex is not None and codex.env:
        for key in ("HOME", "USERPROFILE", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PATH"):
            if codex.env.get(key):
                env[key] = codex.env[key]
                shared_keys.add(key)
    if "HOME" not in shared_keys:
        env["HOME"] = str(worker / "home")
    if "USERPROFILE" not in shared_keys:
        env["USERPROFILE"] = str(worker / "home")
    if "CODEX_HOME" not in shared_keys:
        env["CODEX_HOME"] = str(worker / ".codex")
    env["TMPDIR"] = str(worker / "tmp")
    env["TEMP"] = str(worker / "tmp")
    env["TMP"] = str(worker / "tmp")
    env["XDG_CACHE_HOME"] = str(worker / "cache")
    env["npm_config_cache"] = str(worker / "cache" / "npm")
    env["PIP_CACHE_DIR"] = str(worker / "cache" / "pip")
    env["PYTHONPYCACHEPREFIX"] = str(worker / "cache" / "pycache")
    env.pop("CODEGRAPH_DIR", None)
    return env


def blocked_repro_result(issue_id: str, reason: str) -> dict:
    return {
        "candidate_id": issue_id,
        "status": "blocked",
        "level": "L0",
        "summary": reason,
        "commands_run": [],
        "files_written": [],
        "proof": {"type": "none", "expected": "", "actual": "", "log_excerpt": ""},
        "graph_path_exercised": False,
        "why_valid": "",
        "why_not_reproduced": reason,
        "safety_notes": "",
    }


def git_status_porcelain(path: Path, ignore_prefixes: tuple[str, ...] = ()) -> list[str]:
    if not (path / ".git").exists():
        return []
    result = run_process(["git", "status", "--porcelain"], cwd=path, timeout=60)
    if result.returncode != 0:
        return [f"git status failed: {(result.stderr or result.stdout)[-200:]}"]
    lines = result.stdout.splitlines()
    if not ignore_prefixes:
        return lines
    return [line for line in lines if not _status_path(line).startswith(ignore_prefixes)]


def copy_slice_context(run: Path, worker: Path, candidate: dict) -> None:
    source_task = candidate.get("source_task") if isinstance(candidate.get("source_task"), dict) else {}
    graph = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    slice_id = str(source_task.get("slice_id") or graph.get("slice_id") or "")
    if not slice_id:
        return
    context = run / "slices" / f"{slice_id}.context.md"
    if context.is_file():
        shutil.copyfile(context, worker / "slice.context.md")


def checkout_relative(path: Path) -> str:
    parts = path.as_posix().split("/.codereview/", 1)
    return f".codereview/{parts[1]}" if len(parts) == 2 else ".codereview/runs"


def _status_path(line: str) -> str:
    text = line[3:] if len(line) > 3 else line
    if " -> " in text:
        text = text.rsplit(" -> ", 1)[-1]
    return text.strip()


def process_failure_reason(stage: str, result: object) -> str:
    returncode = getattr(result, "returncode", "")
    timed_out = getattr(result, "timed_out", False)
    timeout_text = " timed out" if timed_out else ""
    return f"{stage}{timeout_text} failed with exit code {returncode}: {compact_process_output(result)}"
