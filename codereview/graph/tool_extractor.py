from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig, auxiliary_codex_config
from ..inventory.git_inventory import analyzable_files
from ..utils.jsonl import read_json_strict, write_json, write_text
from ..utils.paths import ensure_dir
from ..utils.process import ProcessResult, compact_process_output, run_process
from .audit import audit_graph
from .merge import merge_graph_results, normalize_graph_for_inventory


ProgressCallback = Callable[[dict], None]

SCRIPT_MAX_CHARS = 200_000
PROMPT_SCRIPT_MAX_CHARS = 100_000
PROMPT_FAILURE_MAX_CHARS = 40_000


def run_graph_tool_extractor(
    checkout: Path,
    run: Path,
    inventory: dict,
    census: dict,
    graph_tasks: list[dict],
    config: ReviewConfig,
    *,
    progress: ProgressCallback | None = None,
) -> dict | None:
    if not getattr(config.graph, "codex_tool_extractor", True):
        return None
    max_rounds = max(0, int(getattr(config.graph, "tool_extractor_max_rounds", 3)))
    if max_rounds <= 0:
        return None

    worker = run / "workers" / "graph-tool-extractor"
    ensure_dir(worker)
    inventory_path = worker / "inventory.json"
    task_path = worker / "task.json"
    write_json(inventory_path, inventory)
    write_json(task_path, _tool_task_payload(inventory, census, graph_tasks, config))

    history: list[dict] = []
    previous_script = ""
    for round_index in range(max_rounds):
        attempt_number = round_index + 1
        _emit_progress(
            progress,
            f"Graph: Python extractor {'repair' if history else 'generation'} {attempt_number}/{max_rounds}",
            attempt_number,
            max_rounds,
        )
        generation = _generate_extractor_script(
            checkout,
            worker,
            task_path,
            history,
            previous_script,
            attempt_number,
            config,
        )
        if not generation.get("ok"):
            history.append(generation)
            _write_history(worker, history, status="failed" if attempt_number == max_rounds else "retrying")
            continue

        script = str(generation.get("script") or "")
        previous_script = script
        script_path = worker / f"extractor-round-{attempt_number:02d}.py"
        output_path = worker / f"graph-round-{attempt_number:02d}.json"
        write_text(script_path, script)

        _emit_progress(progress, f"Graph: running Python extractor {attempt_number}/{max_rounds}", attempt_number, max_rounds)
        process = _run_extractor_script(checkout, worker, script_path, inventory_path, output_path, config)
        validation = _validate_extractor_output(checkout, output_path, process, inventory)
        attempt = {
            "round": attempt_number,
            "ok": bool(validation.get("ok")),
            "script_path": str(script_path),
            "output_path": str(output_path),
            "generation": _without_script(generation),
            "process": process.to_dict(),
            "validation": {key: value for key, value in validation.items() if key != "result"},
        }
        history.append(attempt)
        _write_history(worker, history, status="ok" if validation.get("ok") else "retrying")
        if validation.get("ok"):
            result = dict(validation["result"])
            result["tool_extractor"] = {
                "worker": "graph-tool-extractor",
                "round": attempt_number,
                "script_path": str(script_path),
                "history_path": str(worker / "history.json"),
            }
            write_json(worker / "result.json", result)
            return result

    _write_history(worker, history, status="failed")
    return None


def _generate_extractor_script(
    checkout: Path,
    worker: Path,
    task_path: Path,
    history: list[dict],
    previous_script: str,
    attempt_number: int,
    config: ReviewConfig,
) -> dict:
    output = worker / f"proposal-round-{attempt_number:02d}.json"
    events = worker / f"proposal-round-{attempt_number:02d}.events.jsonl"
    prompt = _extractor_prompt(checkout, task_path, history, previous_script)
    codex_config = auxiliary_codex_config(config)
    process = run_codex_turn(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "graph-extractor-tool.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=max(30, int(getattr(config.graph, "graph_timeout_seconds", 960))),
        config=codex_config,
        env=base_env(checkout, codex_config),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    if process.returncode != 0:
        return {
            "round": attempt_number,
            "ok": False,
            "phase": "generate",
            "reason": f"Codex extractor generation exited {process.returncode}: {compact_process_output(process)}",
            "process": process_payload,
        }
    try:
        parsed = read_json_strict(output) if output.is_file() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "round": attempt_number,
            "ok": False,
            "phase": "generate",
            "reason": f"Codex extractor generation produced invalid JSON: {exc}",
            "process": process_payload,
        }
    if not isinstance(parsed, dict):
        return {
            "round": attempt_number,
            "ok": False,
            "phase": "generate",
            "reason": "Codex extractor generation produced non-object JSON",
            "process": process_payload,
        }
    script = str(parsed.get("script") or "")
    script_error = _script_error(script)
    if script_error:
        return {
            "round": attempt_number,
            "ok": False,
            "phase": "generate",
            "reason": script_error,
            "process": process_payload,
        }
    return {
        "round": attempt_number,
        "ok": True,
        "phase": "generate",
        "script": script,
        "summary": str(parsed.get("summary") or ""),
        "assumptions": [str(item) for item in parsed.get("assumptions", []) if str(item)] if isinstance(parsed.get("assumptions"), list) else [],
        "process": process_payload,
    }


def _run_extractor_script(
    checkout: Path,
    worker: Path,
    script_path: Path,
    inventory_path: Path,
    output_path: Path,
    config: ReviewConfig,
) -> ProcessResult:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PULLWISE_GRAPH_EXTRACTOR": "1",
        }
    )
    return run_process(
        [
            sys.executable,
            str(script_path),
            "--repo",
            str(checkout),
            "--inventory",
            str(inventory_path),
            "--output",
            str(output_path),
        ],
        cwd=checkout,
        env=env,
        timeout=max(10, int(getattr(config.graph, "tool_extractor_timeout_seconds", 180))),
        log_dir=worker,
    )


def _validate_extractor_output(checkout: Path, output_path: Path, process: ProcessResult, inventory: dict) -> dict:
    if process.returncode != 0:
        return {"ok": False, "phase": "execute", "reason": f"extractor exited {process.returncode}: {compact_process_output(process)}"}
    if not output_path.is_file():
        return {"ok": False, "phase": "execute", "reason": "extractor did not write the output JSON file"}
    try:
        parsed = read_json_strict(output_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "phase": "validate", "reason": f"extractor wrote invalid JSON: {exc}"}
    if not isinstance(parsed, dict):
        return {"ok": False, "phase": "validate", "reason": "extractor output must be a JSON object"}

    result, shape_errors = _normalize_tool_result(parsed, inventory)
    if shape_errors:
        return {"ok": False, "phase": "validate", "reason": "; ".join(shape_errors[:10]), "errors": shape_errors[:50]}

    graph = normalize_graph_for_inventory(merge_graph_results([result]), inventory, checkout)
    normalizer_warnings = [str(item) for item in graph.get("warnings", []) if "graph normalizer dropped" in str(item)]
    if normalizer_warnings:
        return {
            "ok": False,
            "phase": "validate",
            "reason": "; ".join(normalizer_warnings[:5]),
            "warnings": normalizer_warnings[:20],
        }
    audit = audit_graph(graph, inventory, checkout)
    if not audit.get("quality_gate_passed"):
        errors = [str(item) for item in audit.get("quality_errors", []) if str(item)]
        missing = [str(item) for item in audit.get("missing_mapped_files", []) if str(item)]
        reason = "; ".join(errors) or "graph audit quality gate failed"
        if missing:
            reason = f"{reason}; missing_mapped_files={missing[:20]}"
        return {"ok": False, "phase": "validate", "reason": reason, "audit": audit}
    return {"ok": True, "phase": "validate", "result": result, "audit": audit}


def _normalize_tool_result(parsed: dict, inventory: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    required_files = [str(item.get("path") or "") for item in analyzable_files(inventory) if str(item.get("path") or "")]
    required_set = set(required_files)
    for key in ("nodes", "edges", "unresolved_refs", "warnings"):
        if not isinstance(parsed.get(key), list):
            errors.append(f"{key} must be a list")
    coverage = parsed.get("coverage") if isinstance(parsed.get("coverage"), dict) else {}
    assigned = _ordered_paths(coverage.get("assigned_files"))
    mapped = _ordered_paths(coverage.get("mapped_files"))
    if set(assigned) != required_set:
        missing = sorted(required_set - set(assigned))[:20]
        extra = sorted(set(assigned) - required_set)[:20]
        errors.append(f"coverage.assigned_files must match analyzable inventory files; missing={missing}; extra={extra}")
    if set(mapped) != required_set:
        missing = sorted(required_set - set(mapped))[:20]
        extra = sorted(set(mapped) - required_set)[:20]
        errors.append(f"coverage.mapped_files must match analyzable inventory files; missing={missing}; extra={extra}")
    result = {
        "task_id": "graph-tool-extractor",
        "shard_id": "repo-tool-extractor",
        "mapper_index": 1,
        "files": required_files,
        "status": "ok",
        "nodes": [item for item in parsed.get("nodes", []) if isinstance(item, dict)] if isinstance(parsed.get("nodes"), list) else [],
        "edges": [item for item in parsed.get("edges", []) if isinstance(item, dict)] if isinstance(parsed.get("edges"), list) else [],
        "unresolved_refs": [item for item in parsed.get("unresolved_refs", []) if isinstance(item, dict)] if isinstance(parsed.get("unresolved_refs"), list) else [],
        "coverage": {"assigned_files": required_files, "mapped_files": mapped},
        "warnings": [str(item) for item in parsed.get("warnings", []) if str(item)] if isinstance(parsed.get("warnings"), list) else [],
    }
    return result, errors


def _tool_task_payload(inventory: dict, census: dict, graph_tasks: list[dict], config: ReviewConfig) -> dict:
    return {
        "python": {"minimum_version": "3.10", "runtime": "worker sys.executable"},
        "graph_schema_version": getattr(config.graph, "schema_version", "3"),
        "inventory_summary": inventory.get("summary") if isinstance(inventory.get("summary"), dict) else {},
        "files": [
            {
                "path": item.get("path"),
                "scope": item.get("scope"),
                "size_bytes": item.get("size_bytes"),
                "line_count": item.get("line_count"),
                "content_hash": item.get("content_hash"),
                "extension": item.get("extension"),
                "git_status": item.get("git_status"),
                "reason": item.get("reason"),
            }
            for item in inventory.get("files", [])
            if isinstance(item, dict)
        ],
        "census": {
            "languages": census.get("languages"),
            "source_roots": census.get("source_roots"),
            "test_roots": census.get("test_roots"),
            "entrypoint_candidates": census.get("entrypoint_candidates"),
            "census_source": census.get("census_source"),
        },
        "planned_graph_tasks": [
            {
                "task_id": task.get("task_id"),
                "shard_id": task.get("shard_id"),
                "files": task.get("files"),
                "reason": task.get("reason"),
            }
            for task in graph_tasks
            if isinstance(task, dict)
        ],
        "output_contract": {
            "script_args": ["--repo", "--inventory", "--output"],
            "json_shape": "graph-shard.schema.json without task identity fields",
            "required_coverage": "coverage.assigned_files and coverage.mapped_files must exactly match analyzable inventory files",
        },
    }


def _extractor_prompt(checkout: Path, task_path: Path, history: list[dict], previous_script: str) -> str:
    prompt_path = checkout / ".codereview" / "prompts" / "graph-tool-extractor.md"
    prefix = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "Write a Python graph extractor tool."
    payload = read_json_strict(task_path)
    feedback = {
        "previous_failures": _compact_json(history, PROMPT_FAILURE_MAX_CHARS),
        "previous_script": _compact_text(previous_script, PROMPT_SCRIPT_MAX_CHARS),
    }
    return "\n\n".join(
        [
            prefix,
            "Extractor task JSON:",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "Previous execution feedback JSON:",
            "```json",
            json.dumps(feedback, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _script_error(script: str) -> str:
    if not script.strip():
        return "Codex extractor generation returned an empty script"
    if len(script) > SCRIPT_MAX_CHARS:
        return f"Codex extractor script is too large: {len(script)} characters"
    if "\x00" in script:
        return "Codex extractor script contains a NUL byte"
    return ""


def _emit_progress(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
    if progress is None:
        return
    progress({"stage": "graph", "message": message, "current": current, "total": total, "taskId": "graph-tool-extractor"})


def _write_history(worker: Path, history: list[dict], *, status: str) -> None:
    write_json(worker / "history.json", {"status": status, "attempts": history})


def _without_script(value: dict) -> dict:
    payload = dict(value)
    payload.pop("script", None)
    return payload


def _ordered_paths(values: object) -> list[str]:
    paths: list[str] = []
    for value in values if isinstance(values, list) else []:
        text = str(value)
        if text and text not in paths:
            paths.append(text)
    return paths


def _compact_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _compact_json(value: object, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return _compact_text(text, limit)
