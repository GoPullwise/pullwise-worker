from __future__ import annotations

import concurrent.futures
import json
import shutil
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import run_codex_turn
from ..config import CodexConfig, ReviewConfig
from ..utils.paths import ensure_dir, safe_path_component
from ..utils.jsonl import read_json_strict
from ..utils.process import compact_process_output, raise_if_cancelled_callback_exception, run_process
from .event_parser import command_mentioned, event_stream_text
from .filesystem_guard import guard_worker_result
from .worker_dir import create_worker_dir


def run_repro_workers_parallel(
    checkout: Path,
    run: Path,
    candidates: list[dict],
    config: ReviewConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    if not config.repro.enabled:
        return []
    results: list[dict | None] = [None] * len(candidates)
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.repro.max_workers) as executor:
        futures = {
            executor.submit(run_repro_worker, checkout, run, candidate, config): (index, candidate)
            for index, candidate in enumerate(candidates)
        }
        completed = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            index, candidate = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = blocked_repro_worker_exception(run, candidate, exc)
            completed += 1
            _emit_task_progress(
                progress,
                stage="reproduction",
                message=f"Reproduction: candidates {completed}/{total}",
                current=completed,
                total=total,
                task_id=candidate.get("issue_id") or candidate.get("candidate_id"),
            )
    return [result for result in results if result is not None]


def blocked_repro_worker_exception(run: Path, candidate: dict, exc: Exception) -> dict:
    issue_id = safe_path_component(candidate.get("issue_id") or candidate.get("candidate_id"), default="candidate")
    worker = run / "workers" / issue_id
    reason = f"repro worker failed before producing a result: {type(exc).__name__}: {exc}"
    payload = {
        "candidate_id": issue_id,
        "worker": str(worker),
        "process": {"returncode": None, "timed_out": False, "stdout": "", "stderr": reason},
        "result": blocked_repro_result(issue_id, reason),
        "status": "blocked",
        "blocked_reason": reason,
        "filesystem_violations": [],
        "checkout_status_before": [],
        "checkout_status_after": [],
    }
    payload["event_precheck"] = verify_repro_events_and_paths(payload)
    return payload


def _emit_task_progress(
    progress: Callable[[dict], None] | None,
    *,
    stage: str,
    message: str,
    current: int,
    total: int,
    task_id: object,
) -> None:
    if progress is None:
        return
    try:
        progress(
            {
                "stage": stage,
                "message": message,
                "current": current,
                "total": total,
                "taskId": str(task_id or ""),
            }
        )
    except Exception as exc:
        raise_if_cancelled_callback_exception(exc)
        return


def run_repro_worker(checkout: Path, run: Path, candidate: dict, config: ReviewConfig) -> dict:
    issue_id = safe_path_component(candidate.get("issue_id") or candidate.get("candidate_id"), default="candidate")
    worker = run / "workers" / issue_id
    checkout_status_before = git_status_porcelain(
        checkout,
        ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/"),
    )
    create_worker_dir(checkout, worker, candidate)
    copy_review_unit_context(run, worker, candidate)
    prompt = (checkout / ".codereview" / "prompts" / "repro_worker.md").read_text(encoding="utf-8")
    output = worker / "result.json"
    events = worker / "events.jsonl"
    process = run_codex_turn(
        cd=worker,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "repro_result.schema.json",
        output_file=output,
        sandbox="workspace-write",
        timeout_seconds=config.repro.timeout_seconds,
        config=config.codex,
        env=worker_env(worker, config.codex),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    if process.returncode != 0:
        checkout_status_after = git_status_porcelain(
            checkout,
            ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/"),
        )
        violations: list[str] = []
        if checkout_status_after != checkout_status_before:
            violations.append("snapshot checkout changed during repro worker execution")
        failure = process_failure_reason("repro codex turn", process)
        return {
            "candidate_id": issue_id,
            "worker": str(worker),
            "process": process_payload,
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
            parsed = read_json_strict(output)
        except (OSError, json.JSONDecodeError) as exc:
            parsed = blocked_repro_result(issue_id, f"repro produced invalid JSON: {exc}")
    else:
        parsed = blocked_repro_result(issue_id, "repro did not produce an output file")
    violations = [*guard_worker_result(worker, parsed), *validate_repro_result(parsed, expected_candidate_id=issue_id)]
    checkout_status_after = git_status_porcelain(
        checkout,
        ignore_prefixes=(f"{checkout_relative(run)}/", ".codereview/runs/"),
    )
    if checkout_status_after != checkout_status_before:
        violations.append("snapshot checkout changed during repro worker execution")
    payload = {
        "candidate_id": issue_id,
        "worker": str(worker),
        "process": process_payload,
        "result": parsed,
        "status": str(parsed.get("status") or "unknown") if isinstance(parsed, dict) else "unknown",
        "blocked_reason": str(parsed.get("why_not_reproduced") or parsed.get("summary") or "") if isinstance(parsed, dict) and parsed.get("status") == "blocked" else "",
        "filesystem_violations": violations,
        "checkout_status_before": checkout_status_before,
        "checkout_status_after": checkout_status_after,
    }
    payload["event_precheck"] = verify_repro_events_and_paths(payload)
    return payload


def worker_env(worker: Path, codex: CodexConfig | None = None) -> dict[str, str]:
    import os

    env = dict(codex.env) if codex is not None and codex.env else os.environ.copy()
    for child in ("home", "tmp", "cache", "cache/npm", "cache/pip", "cache/pycache"):
        ensure_dir(worker / child)
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

def validate_repro_result(result: dict, *, expected_candidate_id: str) -> list[str]:
    violations: list[str] = []
    if not isinstance(result, dict):
        return ["repro result must be an object"]
    candidate_id = safe_path_component(result.get("candidate_id"), default="")
    if candidate_id != expected_candidate_id:
        violations.append("repro result candidate_id does not match candidate")
    status = str(result.get("status") or "").strip().lower()
    if status not in {"reproduced", "blocked", "not_reproduced", "static_proof"}:
        violations.append("repro result status is unsupported")
    commands = result.get("commands_run") if isinstance(result.get("commands_run"), list) else []
    if status == "reproduced" and not commands:
        violations.append("reproduced result must include commands_run evidence")
    for command in commands:
        if not isinstance(command, dict):
            violations.append("commands_run entries must be objects")
            continue
        if not str(command.get("cmd") or command.get("command") or "").strip():
            violations.append("commands_run entry is missing command text")
        if "exit_code" not in command and "exitCode" not in command:
            violations.append("commands_run entry is missing exit code")
        if not str(command.get("log_path") or command.get("logPath") or "").strip():
            violations.append("commands_run entry is missing log_path")
    return violations


def verify_repro_events_and_paths(payload: dict) -> dict:
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    events_path = Path(str(process.get("events_path") or ""))
    events_text = event_stream_text(events_path) if str(events_path) else ""
    commands = result.get("commands_run") if isinstance(result.get("commands_run"), list) else []
    missing_commands: list[str] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        command_text = str(command.get("cmd") or command.get("command") or "").strip()
        if command_text and not command_mentioned(events_text, command_text):
            missing_commands.append(command_text[:500])
    return {
        "events_path": str(events_path) if str(events_path) else "",
        "events_readable": bool(events_text),
        "commands_checked": len(commands),
        "missing_commands": missing_commands[:20],
        "ok": bool(events_text) and not missing_commands,
    }


def unit_file_stem(unit_id: object) -> str:
    return safe_path_component(unit_id, default="unit")

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


def copy_review_unit_context(run: Path, worker: Path, candidate: dict) -> None:
    source_task = candidate.get("source_task") if isinstance(candidate.get("source_task"), dict) else {}
    graph = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    unit_id = str(source_task.get("unit_id") or graph.get("unit_id") or "")
    if not unit_id:
        return
    context = run / "artifacts" / "review-units" / f"{unit_file_stem(unit_id)}.context.md"
    if context.is_file():
        shutil.copyfile(context, worker / "review-unit.context.md")


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

