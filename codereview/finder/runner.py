from __future__ import annotations

import json
import concurrent.futures
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig
from ..graph.ids import short_hash
from ..units.context import unit_file_stem
from ..utils.jsonl import read_json_strict, write_json, write_text
from ..utils.paths import ensure_dir
from ..utils.process import compact_process_output, raise_if_cancelled_callback_exception
from ..utils.text import read_bounded_text
from .tasks import FinderTask


MAX_FINDER_SUBAGENTS = 6
FINDER_PROMPT_MAX_BYTES = 256 * 1024
FINDER_CONTEXT_PACK_MAX_BYTES = 2 * 1024 * 1024


def run_finders_parallel(
    checkout: Path,
    run: Path,
    tasks: list[FinderTask],
    config: ReviewConfig,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    if not config.finders.enabled:
        return []
    completed = 0
    total = len(tasks)
    batches = list(enumerate(finder_turn_indexed_batches(tasks, run, config), start=1))
    results_by_index: list[dict | None] = [None] * len(tasks)
    max_workers = min(len(batches), finder_turn_parallel(config)) if batches else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                run_finder_batch,
                checkout,
                run,
                [task for _index, task in indexed_batch],
                config,
                batch_index=batch_index,
            ): indexed_batch
            for batch_index, indexed_batch in batches
        }
        for future in concurrent.futures.as_completed(futures):
            indexed_batch = futures[future]
            batch_results = future.result()
            for (index, task), result in zip(indexed_batch, batch_results):
                results_by_index[index] = result
                completed += 1
                _emit_task_progress(
                    progress,
                    stage="finder",
                    message=f"Finder: review tasks {completed}/{total}",
                    current=completed,
                    total=total,
                    task_id=f"{task.focus}:{task.unit_id}",
                )
    return [result for result in results_by_index if result is not None]


def finder_batch_size(config: ReviewConfig) -> int:
    return max(1, min(MAX_FINDER_SUBAGENTS, int(getattr(config.finders, "max_workers", 1) or 1)))


def finder_turn_parallel(config: ReviewConfig) -> int:
    return max(1, min(6, int(getattr(config.finders, "turn_parallel", 1) or 1)))


def finder_max_turns_per_scan(config: ReviewConfig) -> int:
    return max(1, int(getattr(config.finders, "max_turns_per_scan", 3) or 3))


def finder_target_jobs_per_subagent(config: ReviewConfig) -> int:
    return max(1, int(getattr(config.finders, "max_jobs_per_subagent", 18) or 18))


def finder_turn_indexed_batches(tasks: list[FinderTask], run: Path | None, config: ReviewConfig) -> list[list[tuple[int, FinderTask]]]:
    if not tasks:
        return []
    single_turn_capacity = finder_batch_size(config) * finder_target_jobs_per_subagent(config)
    needed_turns = max(1, (len(tasks) + single_turn_capacity - 1) // single_turn_capacity)
    max_turns = min(len(tasks), finder_max_turns_per_scan(config), needed_turns)
    target_turn_jobs = max(single_turn_capacity, (len(tasks) + max_turns - 1) // max_turns)
    groups = []
    for group in finder_grouped_indexed_tasks(tasks, run):
        for start in range(0, len(group), target_turn_jobs):
            groups.append(group[start : start + target_turn_jobs])
    return _pack_indexed_groups(groups, max_turns)


def estimate_finder_turns(tasks: list[FinderTask], run: Path | None, config: ReviewConfig) -> int:
    return len(finder_turn_indexed_batches(tasks, run, config))


def finder_batches(tasks: list[FinderTask], size: int, run: Path | None = None) -> list[list[FinderTask]]:
    return [[task for _index, task in batch] for batch in finder_indexed_batches(tasks, size, run)]


def finder_indexed_batches(tasks: list[FinderTask], size: int, run: Path | None = None) -> list[list[tuple[int, FinderTask]]]:
    size = max(1, int(size or 1))
    grouped = finder_grouped_indexed_tasks(tasks, run)
    batches: list[list[tuple[int, FinderTask]]] = []
    current: list[tuple[int, FinderTask]] = []
    current_family = ""
    for group in grouped:
        family = finder_batch_group_family(finder_batch_group_key(run, group[0][1])) if group else ""
        for start in range(0, len(group), size):
            chunk = group[start : start + size]
            if len(chunk) == size:
                if current:
                    batches.append(current)
                    current = []
                    current_family = ""
                batches.append(chunk)
                continue
            if current and (current_family != family or len(current) + len(chunk) > size):
                batches.append(current)
                current = []
                current_family = ""
            if not current:
                current_family = family
            current.extend(chunk)
    if current:
        batches.append(current)
    return batches


def finder_grouped_indexed_tasks(tasks: list[FinderTask], run: Path | None) -> list[list[tuple[int, FinderTask]]]:
    grouped: dict[str, list[tuple[int, FinderTask]]] = {}
    group_order: list[str] = []
    for index, task in enumerate(tasks):
        key = finder_batch_group_key(run, task)
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append((index, task))
    return [grouped[key] for key in group_order]


def _pack_indexed_groups(groups: list[list[tuple[int, FinderTask]]], max_bins: int) -> list[list[tuple[int, FinderTask]]]:
    bins: list[list[tuple[int, FinderTask]]] = [[] for _ in range(max(1, max_bins))]
    weights = [0 for _ in bins]
    ordered_groups = sorted(groups, key=lambda group: (-len(group), group[0][0] if group else 0))
    for group in ordered_groups:
        if not group:
            continue
        target = min(range(len(bins)), key=lambda index: (weights[index], index))
        bins[target].extend(group)
        weights[target] += len(group)
    packed = [sorted(batch, key=lambda item: item[0]) for batch in bins if batch]
    return sorted(packed, key=lambda batch: batch[0][0])


def finder_batch_group_family(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else key


def finder_batch_group_key(run: Path | None, task: FinderTask) -> str:
    unit = read_review_unit(run, task.unit_id)
    unit_type = str(unit.get("unit_type") or task.unit_type or "component")
    if unit_type == "cross_boundary":
        return f"cross_boundary:{task.unit_id}"
    if unit_type == "global_invariant":
        return f"global:{unit.get('symbol') or task.unit_id}"
    module = review_unit_module_key(unit)
    if module:
        return f"{unit_type}:{module}"
    tags = ",".join(str(tag) for tag in (task.risk_tags or unit.get("risk_tags") or []) if str(tag))
    return f"{unit_type}:{tags or 'default'}"


def read_review_unit(run: Path | None, unit_id: str) -> dict:
    if run is None:
        return {}
    path = run / "artifacts" / "review-units" / f"{unit_file_stem(unit_id)}.json"
    if not path.is_file():
        return {}
    try:
        parsed = read_json_strict(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def review_unit_module_key(unit: dict) -> str:
    paths = review_unit_context_paths(unit)
    if not paths:
        return ""
    modules = sorted({module_key_for_path(path) for path in paths if module_key_for_path(path)})
    return modules[0] if len(modules) == 1 else "+".join(modules[:2])


def review_unit_context_paths(unit: dict) -> list[str]:
    paths: list[str] = []
    context_files = unit.get("context_files") if isinstance(unit.get("context_files"), list) else []
    for item in context_files:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
        else:
            path = str(item or "")
        if path:
            paths.append(path)
    context = unit.get("context") if isinstance(unit.get("context"), dict) else {}
    files = context.get("files") if isinstance(context.get("files"), list) else []
    paths.extend(str(path) for path in files if str(path))
    return sorted(dict.fromkeys(paths))


def module_key_for_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts:
        return ""
    if len(parts) == 1:
        return "."
    if "." in parts[1]:
        return parts[0]
    return "/".join(parts[:2])


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


def run_finder_batch(checkout: Path, run: Path, tasks: list[FinderTask], config: ReviewConfig, *, batch_index: int = 1) -> list[dict]:
    if not tasks:
        return []
    worker = run / "finder-batches" / finder_batch_id(tasks, batch_index=batch_index)
    ensure_dir(worker)
    try:
        payload = finder_batch_payload(checkout, run, tasks, config)
        prompt = finder_batch_prompt(checkout, payload)
    except OSError as exc:
        process_payload = {
            "command": ["codex", "app-server", "turn/start"],
            "cwd": str(checkout),
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": 0,
            "timed_out": False,
            "stdout_path": "",
            "stderr_path": "",
            "queueWaitMs": 0,
            "execDurationMs": 0,
        }
        write_json(worker / "process.json", process_payload)
        return blocked_finder_batch_results(tasks, process_payload, f"finder batch input unreadable: {exc}")
    write_json(worker / "task.json", payload)
    write_text(worker / "prompt.md", prompt)
    output = worker / "result.json"
    events = worker / "events.jsonl"
    process = run_codex_turn(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "finder-batch.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=finder_batch_timeout_seconds(len(tasks), config),
        config=config.codex,
        env=base_env(checkout, config.codex),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    write_json(worker / "process.json", process_payload)
    if process.returncode != 0:
        return blocked_finder_batch_results(tasks, process_payload, process_failure_reason("finder batch codex turn", process))
    if not output.is_file():
        return blocked_finder_batch_results(tasks, process_payload, "finder batch did not produce an output file")
    try:
        parsed = read_json_strict(output)
    except (OSError, json.JSONDecodeError) as exc:
        return blocked_finder_batch_results(tasks, process_payload, f"finder batch produced invalid JSON: {exc}")
    return finder_batch_results(run, tasks, parsed, process_payload)


def finder_batch_id(tasks: list[FinderTask], *, batch_index: int) -> str:
    key = [
        {"unit_id": task.unit_id, "focus": task.focus, "unit_type": task.unit_type, "review_pass": task.review_pass}
        for task in tasks
    ]
    return f"finder-batch-{batch_index:04d}-{short_hash(key, length=12)}"


def finder_batch_timeout_seconds(task_count: int, config: ReviewConfig) -> int:
    subagent_capacity = finder_batch_size(config) * finder_target_jobs_per_subagent(config)
    waves = max(1, (max(1, task_count) + subagent_capacity - 1) // subagent_capacity)
    return max(60, int(getattr(config.finders, "timeout_seconds", 600) or 600)) * waves


def finder_batch_payload(checkout: Path, run: Path, tasks: list[FinderTask], config: ReviewConfig) -> dict:
    focus_prompts = {
        focus: read_bounded_text(checkout / ".codereview" / "prompts" / f"finder_{focus}.md", max_bytes=FINDER_PROMPT_MAX_BYTES)
        for focus in sorted({task.focus for task in tasks})
    }
    jobs = []
    job_groups = finder_subagent_job_groups(run, tasks, config)
    group_by_job = {job_id: str(group.get("group_id") or "") for group in job_groups for job_id in group.get("jobs", [])}
    context_packs: dict[str, str] = {}
    for task in tasks:
        task_id = finder_task_id(task)
        stem = unit_file_stem(task.unit_id)
        context_file = run / "artifacts" / "review-units" / f"{stem}.context.md"
        context_packs.setdefault(task.unit_id, read_bounded_text(context_file, max_bytes=FINDER_CONTEXT_PACK_MAX_BYTES))
        jobs.append(
            {
                "task_id": task_id,
                "unit_id": task.unit_id,
                "focus": task.focus,
                "group_id": group_by_job.get(task_id, ""),
                "module_key": finder_batch_group_key(run, task),
                "unit_type": task.unit_type,
                "review_pass": task.review_pass,
                "risk_tags": task.risk_tags or [],
                "context_pack_id": task.unit_id,
            }
        )
    return {
        "finder_subagent_limit": finder_batch_size(config),
        "job_groups": job_groups,
        "context_packs": context_packs,
        "focus_prompts": focus_prompts,
        "jobs": jobs,
    }


def finder_subagent_job_groups(run: Path, tasks: list[FinderTask], config: ReviewConfig) -> list[dict]:
    if not tasks:
        return []
    subagent_limit = finder_batch_size(config)
    target_jobs = max(finder_target_jobs_per_subagent(config), (len(tasks) + subagent_limit - 1) // subagent_limit)
    split_groups: list[list[tuple[int, FinderTask]]] = []
    for group in finder_grouped_indexed_tasks(tasks, run):
        for start in range(0, len(group), target_jobs):
            split_groups.append(group[start : start + target_jobs])
    packed = _pack_indexed_groups(split_groups, subagent_limit)
    output = []
    for index, batch in enumerate(packed, start=1):
        batch_tasks = [task for _position, task in sorted(batch, key=lambda item: item[0])]
        module_keys = sorted(dict.fromkeys(finder_batch_group_key(run, task) for task in batch_tasks))
        output.append(
            {
                "group_id": f"finder-group-{index:02d}",
                "job_count": len(batch_tasks),
                "unit_count": len({task.unit_id for task in batch_tasks}),
                "focuses": sorted(dict.fromkeys(task.focus for task in batch_tasks)),
                "module_keys": module_keys,
                "risk_tags": sorted({tag for task in batch_tasks for tag in (task.risk_tags or [])}),
                "context_pack_ids": sorted(dict.fromkeys(task.unit_id for task in batch_tasks)),
                "jobs": [finder_task_id(task) for task in batch_tasks],
            }
        )
    return output


def finder_batch_prompt(checkout: Path, payload: dict) -> str:
    prompt_file = checkout / ".codereview" / "prompts" / "finder-batch-coordinator.md"
    prefix = read_bounded_text(prompt_file, max_bytes=FINDER_PROMPT_MAX_BYTES) if prompt_file.is_file() else "You are a graph-verified finder coordinator."
    return f"{prefix}\n\nAssigned finder batch JSON:\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n```"


def finder_batch_results(run: Path, tasks: list[FinderTask], parsed: object, process_payload: dict) -> list[dict]:
    results = parsed.get("results") if isinstance(parsed, dict) and isinstance(parsed.get("results"), list) else []
    by_key: dict[tuple[str, str], dict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        by_key[(str(item.get("unit_id") or ""), str(item.get("focus") or ""))] = item
    output: list[dict] = []
    for task in tasks:
        item = by_key.get(finder_task_key(task))
        if item is None:
            output.append(
                {
                    "task": task.__dict__,
                    "process": process_payload,
                    "result": {"unit_id": task.unit_id, "focus": task.focus, "context_requests": [], "candidates": []},
                    "status": "blocked",
                    "blocked_reason": "finder batch did not return a result for this task",
                }
            )
            continue
        result = normalize_finder_result(item, task)
        write_individual_finder_output(run, task, result)
        output.append({"task": task.__dict__, "process": process_payload, "result": result, "status": "ok"})
    return output


def normalize_finder_result(item: dict, task: FinderTask) -> dict:
    candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
    context_requests = item.get("context_requests") if isinstance(item.get("context_requests"), list) else []
    return {
        "unit_id": task.unit_id,
        "focus": task.focus,
        "context_requests": context_requests,
        "candidates": candidates,
    }


def write_individual_finder_output(run: Path, task: FinderTask, result: dict) -> None:
    stem = unit_file_stem(task.unit_id)
    output = run / "finder" / task.focus / f"{stem}.result.json"
    write_json(output, result)


def blocked_finder_batch_results(tasks: list[FinderTask], process_payload: dict, reason: str) -> list[dict]:
    return [
        {
            "task": task.__dict__,
            "process": process_payload,
            "result": {"unit_id": task.unit_id, "focus": task.focus, "context_requests": [], "candidates": []},
            "status": "blocked",
            "blocked_reason": reason,
        }
        for task in tasks
    ]


def finder_task_id(task: FinderTask) -> str:
    return f"{task.focus}:{task.unit_id}"


def finder_task_key(task: FinderTask) -> tuple[str, str]:
    return (str(task.unit_id or ""), str(task.focus or ""))


def run_finder(checkout: Path, run: Path, task: FinderTask, config: ReviewConfig) -> dict:
    prompt_file = checkout / ".codereview" / "prompts" / f"finder_{task.focus}.md"
    stem = unit_file_stem(task.unit_id)
    context_file = run / "artifacts" / "review-units" / f"{stem}.context.md"
    try:
        prompt = (
            read_bounded_text(prompt_file, max_bytes=FINDER_PROMPT_MAX_BYTES)
            + "\n\nInput context pack:\n"
            + read_bounded_text(context_file, max_bytes=FINDER_CONTEXT_PACK_MAX_BYTES)
        )
    except OSError as exc:
        return {
            "task": task.__dict__,
            "process": {},
            "result": {"candidates": []},
            "status": "blocked",
            "blocked_reason": f"finder input unreadable: {exc}",
        }
    output = run / "finder" / task.focus / f"{stem}.result.json"
    ensure_dir(output.parent)
    events = output.with_suffix(".events.jsonl")
    result = run_codex_turn(
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
            "blocked_reason": process_failure_reason("finder codex turn", result),
        }
    parsed = {}
    if output.is_file():
        try:
            parsed = read_json_strict(output)
        except (OSError, json.JSONDecodeError) as exc:
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
