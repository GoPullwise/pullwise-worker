from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig
from ..utils.paths import safe_path_component
from .validate import local_judge, validate_judge_result


def run_judges_parallel(
    run: Path,
    candidates: list[dict],
    repro_results: list[dict],
    checkout: Path,
    config: ReviewConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    by_id = {str(item.get("issue_id") or ""): item for item in candidates}
    results: list[dict | None] = [None] * len(repro_results)
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.repro.max_workers) as executor:
        futures = {
            executor.submit(run_judge, run, by_id.get(str(repro.get("candidate_id") or ""), {}), repro, checkout, config): (
                index,
                repro,
            )
            for index, repro in enumerate(repro_results)
        }
        completed = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            index, repro = futures[future]
            results[index] = future.result()
            completed += 1
            _emit_task_progress(
                progress,
                stage="judge",
                message=f"Judge: candidates {completed}/{total}",
                current=completed,
                total=total,
                task_id=repro.get("candidate_id"),
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


def run_judge(run: Path, candidate: dict, repro: dict, checkout: Path, config: ReviewConfig) -> dict:
    local = local_judge(candidate, repro)
    if local.get("safe_to_show_user") is True and config.repro.require_red_green and local.get("level") != "L3":
        local = {
            **local,
            "status": "rejected",
            "level": "L0",
            "safe_to_show_user": False,
            "reason": "red-green verification required but reproduction level was not L3",
        }
    if local.get("safe_to_show_user") is not True:
        return local
    prompt_file = checkout / ".codereview" / "prompts" / "judge.md"
    schema = checkout / ".codereview" / "schemas" / "judge_result.schema.json"
    if not prompt_file.is_file() or not schema.is_file():
        return local
    output = run / "judge" / f"{safe_path_component(local['candidate_id'], default='candidate')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    events = output.with_suffix(".events.jsonl")
    prompt = "\n\n".join(
        [
            prompt_file.read_text(encoding="utf-8"),
            "Candidate JSON:",
            json.dumps(candidate, ensure_ascii=False, indent=2),
            "Repro result JSON:",
            json.dumps(repro, ensure_ascii=False, indent=2),
            "Local gate result JSON:",
            json.dumps(local, ensure_ascii=False, indent=2),
        ]
    )
    process = run_codex_exec(
        cd=checkout,
        prompt=prompt,
        output_schema=schema,
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.codex.timeout_seconds,
        config=config.codex,
        env=base_env(checkout, config.codex),
        events_file=events,
    )
    if process.returncode != 0 or not output.is_file():
        return local
    try:
        parsed = json.loads(output.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return local
    violations = validate_judge_result(parsed, expected_candidate_id=str(local.get("candidate_id") or ""))
    if violations:
        return local
    if parsed.get("safe_to_show_user") is True and parsed.get("status") == "confirmed":
        parsed["evidence_summary"] = local.get("evidence_summary") if isinstance(local.get("evidence_summary"), dict) else parsed.get("evidence_summary")
        return parsed
    return parsed if isinstance(parsed, dict) else local
