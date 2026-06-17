from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig
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
        env=base_env(checkout),
    )
    parsed = {}
    if output.is_file():
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = {}
    return {"task": task.__dict__, "process": result.to_dict(), "result": parsed}
