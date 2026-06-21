from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig
from ..units.context import unit_file_stem
from ..utils.process import compact_process_output
from .tasks import FinderTask


def run_finders_parallel(
    checkout: Path,
    run: Path,
    tasks: list[FinderTask],
    config: ReviewConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    if not config.finders.enabled:
        return []
    results: list[dict | None] = [None] * len(tasks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.finders.max_workers) as executor:
        futures = {
            executor.submit(run_finder, checkout, run, task, config): (index, task)
            for index, task in enumerate(tasks)
        }
        completed = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            index, task = futures[future]
            results[index] = future.result()
            completed += 1
            _emit_task_progress(
                progress,
                stage="finder",
                message=f"Finder: review tasks {completed}/{total}",
                current=completed,
                total=total,
                task_id=f"{task.focus}:{task.unit_id}",
            )
    return [result for result in results if result is not None]


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
    except Exception:
        return


def run_finder(checkout: Path, run: Path, task: FinderTask, config: ReviewConfig) -> dict:
    prompt_file = checkout / ".codereview" / "prompts" / f"finder_{task.focus}.md"
    stem = unit_file_stem(task.unit_id)
    context_file = run / "artifacts" / "review-units" / f"{stem}.context.md"
    prompt = prompt_file.read_text(encoding="utf-8") + "\n\nInput context pack:\n" + context_file.read_text(encoding="utf-8")
    output = run / "finder" / task.focus / f"{stem}.result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    events = output.with_suffix(".events.jsonl")
    result = run_codex_exec(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "finder_result.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.finders.timeout_seconds,
        config=config.codex,
        env=base_env(checkout, config.codex),
        events_file=events,
    )
    process_payload = {**result.to_dict(), "events_path": str(events)}
    if result.returncode != 0:
        return {
            "task": task.__dict__,
            "process": process_payload,
            "result": {"candidates": []},
            "status": "blocked",
            "blocked_reason": process_failure_reason("finder codex exec", result),
        }
    parsed = {}
    if output.is_file():
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "task": task.__dict__,
                "process": process_payload,
                "result": {"candidates": []},
                "status": "blocked",
                "blocked_reason": f"finder produced invalid JSON: {exc}",
            }
    if not output.is_file():
        return {
            "task": task.__dict__,
            "process": process_payload,
            "result": {"candidates": []},
            "status": "blocked",
            "blocked_reason": "finder did not produce an output file",
        }
    return {"task": task.__dict__, "process": process_payload, "result": parsed, "status": "ok"}


def process_failure_reason(stage: str, result: object) -> str:
    returncode = getattr(result, "returncode", "")
    timed_out = getattr(result, "timed_out", False)
    timeout_text = " timed out" if timed_out else ""
    return f"{stage}{timeout_text} failed with exit code {returncode}: {compact_process_output(result)}"
