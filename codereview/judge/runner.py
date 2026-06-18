from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig
from ..utils.paths import safe_path_component
from .validate import local_judge, validate_judge_result


def run_judges_parallel(run: Path, candidates: list[dict], repro_results: list[dict], checkout: Path, config: ReviewConfig) -> list[dict]:
    by_id = {str(item.get("issue_id") or ""): item for item in candidates}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.repro.max_workers) as executor:
        futures = [executor.submit(run_judge, run, by_id.get(str(repro.get("candidate_id") or ""), {}), repro, checkout, config) for repro in repro_results]
        return [future.result() for future in futures]


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
        return parsed
    return parsed if isinstance(parsed, dict) else local
