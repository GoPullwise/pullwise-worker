from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig
from ..utils.process import compact_process_output
from .tasks import FinderTask


def run_finders_parallel(checkout: Path, run: Path, tasks: list[FinderTask], config: ReviewConfig) -> list[dict]:
    if not config.finders.enabled:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.finders.max_workers) as executor:
        futures = [executor.submit(run_finder, checkout, run, task, config) for task in tasks]
        return [future.result() for future in futures]


def run_finder(checkout: Path, run: Path, task: FinderTask, config: ReviewConfig) -> dict:
    prompt_file = checkout / ".codereview" / "prompts" / f"finder_{task.focus}.md"
    context_file = run / "slices" / f"{task.slice_id}.context.md"
    prompt = prompt_file.read_text(encoding="utf-8") + "\n\nInput context pack:\n" + context_file.read_text(encoding="utf-8")
    output = run / "finder" / task.focus / f"{task.slice_id}.result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    result = run_codex_exec(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "finder_result.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.finders.timeout_seconds,
        config=config.codex,
        env=base_env(checkout, config.codex),
    )
    if result.returncode != 0:
        return {
            "task": task.__dict__,
            "process": result.to_dict(),
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
                "process": result.to_dict(),
                "result": {"candidates": []},
                "status": "blocked",
                "blocked_reason": f"finder produced invalid JSON: {exc}",
            }
    if not output.is_file():
        return {
            "task": task.__dict__,
            "process": result.to_dict(),
            "result": {"candidates": []},
            "status": "blocked",
            "blocked_reason": "finder did not produce an output file",
        }
    return {"task": task.__dict__, "process": result.to_dict(), "result": parsed, "status": "ok"}


def process_failure_reason(stage: str, result: object) -> str:
    returncode = getattr(result, "returncode", "")
    timed_out = getattr(result, "timed_out", False)
    timeout_text = " timed out" if timed_out else ""
    return f"{stage}{timeout_text} failed with exit code {returncode}: {compact_process_output(result)}"
