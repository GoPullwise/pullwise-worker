from __future__ import annotations

import ast
import concurrent.futures
import json
import re
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_exec
from ..config import ReviewConfig, auxiliary_codex_config
from ..utils.jsonl import read_json, write_json
from .cache import graph_cache_key
from .contracts import confidence_for_evidence, language_for_path, risk_tags_for_path
from .ids import (
    config_node_id,
    dependency_node_id,
    env_node_id,
    file_node_id,
    route_node_id,
    stable_edge_id,
    stable_node_id,
    table_node_id,
)


_JS_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
_JS_IMPORT_RE = re.compile(r"^\s*import\b.*?\bfrom\s+['\"]([^'\"]+)['\"]|^\s*(?:const|let|var)\s+.*?=\s*require\(['\"]([^'\"]+)['\"]\)")
_JS_ROUTE_RE = re.compile(r"\b(?:app|router|server)\.(get|post|put|patch|delete|options|head)\s*\(\s*['\"]([^'\"]+)['\"]")
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_SQL_TABLE_RE = re.compile(r"\b(?:from|into|update|join)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)


def map_graph_tasks(
    checkout: Path,
    tasks: list[dict],
    inventory: dict,
    config: ReviewConfig,
    run: Path | None = None,
    progress: Callable[[dict], None] | None = None,
    progress_label: str = "Graph: mapping shards",
) -> list[dict]:
    by_path = {str(item.get("path") or ""): item for item in inventory.get("files", []) if isinstance(item, dict)}
    if getattr(config.graph, "codex_mappers", False) and run is not None:
        return map_codex_graph_tasks_with_coordinator(checkout, tasks, by_path, config, run, progress, progress_label)
    max_workers = max(1, int(getattr(config.graph, "map_parallel", 1)))
    results: list[dict | None] = [None] * len(tasks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_map_task_with_policy, checkout, task, by_path, config, run): (index, task)
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
                stage="graph",
                message=f"{progress_label} {completed}/{total}",
                current=completed,
                total=total,
                task_id=task.get("task_id") or task.get("shard_id"),
            )
    return [result for result in results if result is not None]


def map_codex_graph_tasks_with_coordinator(
    checkout: Path,
    tasks: list[dict],
    inventory_by_path: dict[str, dict],
    config: ReviewConfig,
    run: Path,
    progress: Callable[[dict], None] | None,
    progress_label: str,
) -> list[dict]:
    results: list[dict | None] = [None] * len(tasks)
    pending: list[tuple[int, dict]] = []
    completed = 0
    total = len(tasks)
    for index, task in enumerate(tasks):
        cached = _load_cached_task_result(checkout, task, inventory_by_path, config)
        if cached is None:
            pending.append((index, task))
            continue
        results[index] = cached
        completed += 1
        _emit_task_progress(
            progress,
            stage="graph",
            message=f"{progress_label} {completed}/{total}",
            current=completed,
            total=total,
            task_id=task.get("task_id") or task.get("shard_id"),
        )
    if pending:
        mapped = run_codex_graph_mapper_coordinator(
            checkout,
            run,
            [task for _, task in pending],
            inventory_by_path,
            config,
        )
        mapped_by_task = {_task_result_key(result): result for result in mapped if isinstance(result, dict)}
        for index, task in pending:
            result = mapped_by_task.get(_task_key(task))
            if result is None:
                result = _blocked_task_result(task, "codex graph mapper coordinator did not return a result for this task")
            results[index] = result
            if result.get("status") == "ok":
                _save_cached_task_result(checkout, task, inventory_by_path, config, result)
            completed += 1
            _emit_task_progress(
                progress,
                stage="graph",
                message=f"{progress_label} {completed}/{total}",
                current=completed,
                total=total,
                task_id=task.get("task_id") or task.get("shard_id"),
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


def _map_task_with_policy(checkout: Path, task: dict, inventory_by_path: dict[str, dict], config: ReviewConfig, run: Path | None) -> dict:
    cached = _load_cached_task_result(checkout, task, inventory_by_path, config)
    if cached is not None:
        return cached
    if getattr(config.graph, "codex_mappers", False) and run is not None:
        result = run_codex_graph_mapper(checkout, run, task, inventory_by_path, config)
        if result.get("status") == "ok":
            _save_cached_task_result(checkout, task, inventory_by_path, config, result)
        return result
    result = map_graph_task(checkout, task, inventory_by_path)
    _save_cached_task_result(checkout, task, inventory_by_path, config, result)
    return result


def run_codex_graph_mapper(checkout: Path, run: Path, task: dict, inventory_by_path: dict[str, dict], config: ReviewConfig) -> dict:
    task_id = str(task.get("task_id") or "graph-map")
    worker = run / "workers" / task_id
    worker.mkdir(parents=True, exist_ok=True)
    task_payload = {
        **task,
        "files_metadata": [inventory_by_path.get(str(path), {"path": str(path)}) for path in task.get("files", [])],
    }
    prompt = _graph_mapper_prompt(checkout, task_payload)
    write_json(worker / "task.json", task_payload)
    (worker / "prompt.md").write_text(prompt, encoding="utf-8")
    output = worker / "result.json"
    events = worker / "events.jsonl"
    codex_config = auxiliary_codex_config(config)
    process = run_codex_exec(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "graph-shard.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.graph.graph_timeout_seconds,
        config=codex_config,
        env=base_env(checkout, codex_config),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    if process.returncode != 0:
        return {
            "task_id": task_id,
            "shard_id": task.get("shard_id"),
            "files": task.get("files") or [],
            "nodes": [],
            "edges": [],
            "unresolved_refs": [],
            "coverage": {"assigned_files": task.get("files") or [], "mapped_files": []},
            "warnings": [],
            "process": process_payload,
            "status": "blocked",
            "blocked_reason": f"codex graph mapper exited {process.returncode}",
        }
    try:
        parsed = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
    except json.JSONDecodeError as exc:
        parsed = {}
        parse_error = str(exc)
    else:
        parse_error = ""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("nodes"), list):
        return {
            "task_id": task_id,
            "shard_id": task.get("shard_id"),
            "files": task.get("files") or [],
            "nodes": [],
            "edges": [],
            "unresolved_refs": [],
            "coverage": {"assigned_files": task.get("files") or [], "mapped_files": []},
            "warnings": [],
            "process": process_payload,
            "status": "blocked",
            "blocked_reason": parse_error or "codex graph mapper did not produce graph-shard JSON",
        }
    parsed.setdefault("task_id", task_id)
    parsed.setdefault("shard_id", task.get("shard_id"))
    parsed.setdefault("coverage", {"assigned_files": task.get("files") or [], "mapped_files": task.get("files") or []})
    parsed.setdefault("warnings", [])
    parsed["process"] = process_payload
    parsed["status"] = "ok"
    return parsed


def run_codex_graph_mapper_coordinator(
    checkout: Path,
    run: Path,
    tasks: list[dict],
    inventory_by_path: dict[str, dict],
    config: ReviewConfig,
) -> list[dict]:
    worker = run / "workers" / _coordinator_worker_name(tasks)
    worker.mkdir(parents=True, exist_ok=True)
    payload = {
        "mapper_subagent_limit": max(1, int(getattr(config.graph, "mapper_subagent_limit", 6))),
        "jobs": [
            {
                **task,
                "files_metadata": [inventory_by_path.get(str(path), {"path": str(path)}) for path in task.get("files", [])],
            }
            for task in tasks
        ],
    }
    prompt = _graph_mapper_coordinator_prompt(checkout, payload)
    write_json(worker / "task.json", payload)
    (worker / "prompt.md").write_text(prompt, encoding="utf-8")
    output = worker / "result.json"
    events = worker / "events.jsonl"
    codex_config = auxiliary_codex_config(config)
    process = run_codex_exec(
        cd=checkout,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "graph-shard-batch.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=_coordinator_timeout_seconds(tasks, config),
        config=codex_config,
        env=base_env(checkout, codex_config),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    write_json(worker / "process.json", process_payload)
    if process.returncode != 0:
        return [
            _blocked_task_result(task, f"codex graph mapper coordinator exited {process.returncode}", process=process_payload)
            for task in tasks
        ]
    try:
        parsed = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
    except json.JSONDecodeError as exc:
        return [
            _blocked_task_result(task, f"codex graph mapper coordinator produced invalid JSON: {exc}", process=process_payload)
            for task in tasks
        ]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return [
            _blocked_task_result(task, "codex graph mapper coordinator did not produce graph-shard-batch JSON", process=process_payload)
            for task in tasks
        ]
    normalized: list[dict] = []
    task_by_key = {_task_key(task): task for task in tasks}
    for item in parsed.get("results") or []:
        if not isinstance(item, dict):
            continue
        task = task_by_key.get(_task_result_key(item))
        if task is None:
            continue
        result = _normalize_coordinator_task_result(item, task)
        result["process"] = process_payload
        normalized.append(result)
    return normalized


def _graph_mapper_prompt(checkout: Path, task_payload: dict) -> str:
    prompt_path = checkout / ".codereview" / "prompts" / "graph-mapper.md"
    prefix = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "You are a code evidence graph mapper."
    return "\n\n".join(
        [
            prefix,
            "Assigned graph task JSON:",
            "```json",
            json.dumps(task_payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _graph_mapper_coordinator_prompt(checkout: Path, payload: dict) -> str:
    prompt_path = checkout / ".codereview" / "prompts" / "graph-mapper-coordinator.md"
    prefix = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else "You are a graph mapper coordinator."
    return "\n\n".join(
        [
            prefix,
            "Assigned graph mapper coordinator JSON:",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _coordinator_worker_name(tasks: list[dict]) -> str:
    task_ids = [str(task.get("task_id") or task.get("shard_id") or "task") for task in tasks]
    if not task_ids:
        return "graph-map-coordinator-empty"
    first = _worker_name_token(task_ids[0])
    last = _worker_name_token(task_ids[-1])
    return f"graph-map-coordinator-{first}-{last}"


def _worker_name_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:48] or "task"


def _coordinator_timeout_seconds(tasks: list[dict], config: ReviewConfig) -> int:
    limit = max(1, int(getattr(config.graph, "mapper_subagent_limit", 6)))
    waves = max(1, (len(tasks) + limit - 1) // limit)
    return max(30, int(getattr(config.graph, "graph_timeout_seconds", 480))) * waves


def _normalize_coordinator_task_result(item: dict, task: dict) -> dict:
    result = dict(item)
    result["task_id"] = str(task.get("task_id") or result.get("task_id") or "")
    result["shard_id"] = str(task.get("shard_id") or result.get("shard_id") or "")
    result["mapper_index"] = _safe_int(task.get("mapper_index") or result.get("mapper_index"), default=1)
    result["files"] = [str(path) for path in (task.get("files") or result.get("files") or []) if str(path)]
    result.setdefault("nodes", [])
    result.setdefault("edges", [])
    result.setdefault("unresolved_refs", [])
    result.setdefault("warnings", [])
    result.setdefault("status", "ok")
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    result["coverage"] = {
        "assigned_files": [str(path) for path in (coverage.get("assigned_files") or task.get("files") or []) if str(path)],
        "mapped_files": [str(path) for path in (coverage.get("mapped_files") or []) if str(path)],
    }
    return result


def _task_key(task: dict) -> tuple[str, str, int]:
    return (
        str(task.get("task_id") or ""),
        str(task.get("shard_id") or ""),
        _safe_int(task.get("mapper_index"), default=1),
    )


def _task_result_key(result: dict) -> tuple[str, str, int]:
    return (
        str(result.get("task_id") or ""),
        str(result.get("shard_id") or ""),
        _safe_int(result.get("mapper_index"), default=1),
    )


def _safe_int(value: object, *, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _blocked_task_result(task: dict, reason: str, process: dict | None = None) -> dict:
    result = {
        "task_id": str(task.get("task_id") or ""),
        "shard_id": task.get("shard_id"),
        "mapper_index": task.get("mapper_index"),
        "files": task.get("files") or [],
        "nodes": [],
        "edges": [],
        "unresolved_refs": [],
        "coverage": {"assigned_files": task.get("files") or [], "mapped_files": []},
        "warnings": [],
        "status": "blocked",
        "blocked_reason": reason,
    }
    if process is not None:
        result["process"] = process
    return result


def _load_cached_task_result(checkout: Path, task: dict, inventory_by_path: dict[str, dict], config: ReviewConfig) -> dict | None:
    if not getattr(config.graph, "incremental", True) or task.get("double_mapped"):
        return None
    path = _task_cache_path(checkout, task, inventory_by_path, config)
    cached = read_json(path, default=None)
    if not isinstance(cached, dict):
        return None
    cached = dict(cached)
    cached["cache_hit"] = True
    return cached


def _save_cached_task_result(checkout: Path, task: dict, inventory_by_path: dict[str, dict], config: ReviewConfig, result: dict) -> None:
    if not getattr(config.graph, "incremental", True) or task.get("double_mapped"):
        return
    path = _task_cache_path(checkout, task, inventory_by_path, config)
    payload = dict(result)
    payload.pop("process", None)
    payload.pop("codex_process", None)
    payload["cache_key"] = path.stem
    write_json(path, payload)


def _task_cache_path(checkout: Path, task: dict, inventory_by_path: dict[str, dict], config: ReviewConfig) -> Path:
    files = [inventory_by_path.get(str(path), {"path": str(path), "content_hash": ""}) for path in task.get("files", [])]
    key = graph_cache_key(
        {
            "content_hash": "|".join(f"{item.get('path')}={item.get('content_hash')}" for item in files),
        },
        schema_version=config.graph.schema_version,
        prompt_version=config.graph.prompt_version,
        language="mixed-shard",
        profile_name="codex" if config.graph.codex_mappers else "local",
    )
    return checkout / ".codereview" / "graph-cache" / "shard-results" / f"{key}.json"


def map_graph_task(checkout: Path, task: dict, inventory_by_path: dict[str, dict]) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    unresolved: list[dict] = []
    mapped_files: list[str] = []
    warnings: list[str] = []
    for rel in task.get("files", []):
        file_info = inventory_by_path.get(str(rel), {})
        if file_info.get("scope") != "analyze":
            continue
        try:
            result = map_file(checkout, str(rel), file_info, str(task.get("task_id") or "graph-map"))
        except Exception as exc:
            warnings.append(f"{rel}: {exc}")
            result = _fallback_file_result(str(rel), file_info, str(task.get("task_id") or "graph-map"), reason=str(exc))
        nodes.extend(result["nodes"])
        edges.extend(result["edges"])
        unresolved.extend(result["unresolved_refs"])
        mapped_files.append(str(rel))
    return {
        "task_id": task.get("task_id"),
        "shard_id": task.get("shard_id"),
        "mapper_index": task.get("mapper_index"),
        "files": list(task.get("files", [])),
        "nodes": nodes,
        "edges": edges,
        "unresolved_refs": unresolved,
        "coverage": {
            "assigned_files": list(task.get("files", [])),
            "mapped_files": mapped_files,
        },
        "warnings": warnings,
    }


def map_file(checkout: Path, rel: str, file_info: dict, worker_id: str) -> dict:
    path = checkout / rel
    language = language_for_path(rel)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    nodes = [_file_node(rel, file_info, language, worker_id)]
    edges: list[dict] = []
    unresolved: list[dict] = []
    if language == "python":
        py = _map_python(rel, file_info, lines, worker_id)
        nodes.extend(py["nodes"])
        edges.extend(py["edges"])
        unresolved.extend(py["unresolved_refs"])
    elif language in {"javascript", "typescript"}:
        js = _map_javascript(rel, file_info, lines, worker_id, language)
        nodes.extend(js["nodes"])
        edges.extend(js["edges"])
        unresolved.extend(js["unresolved_refs"])
    elif language == "json":
        config = _map_json_config(rel, file_info, lines, worker_id)
        nodes.extend(config["nodes"])
        edges.extend(config["edges"])
    elif language == "sql":
        sql = _map_sql(rel, file_info, lines, worker_id)
        nodes.extend(sql["nodes"])
        edges.extend(sql["edges"])
    return {"nodes": nodes, "edges": edges, "unresolved_refs": unresolved}


def _file_node(rel: str, file_info: dict, language: str, worker_id: str) -> dict:
    kind = "test_file" if _is_test_file(rel) else "file"
    line_count = max(1, int(file_info.get("line_count") or 1))
    return {
        "id": file_node_id(rel),
        "kind": kind,
        "name": Path(rel).name,
        "qualified_name": rel,
        "language": language,
        "file": rel,
        "span": {"start_line": 1, "end_line": line_count},
        "signature": rel,
        "visibility": "repository",
        "content_hash": file_info.get("content_hash") or "",
        "attributes": risk_tags_for_path(rel),
        "evidence": [_evidence(rel, 1, line_count, "direct_syntax", file_info)],
        "generated_by": {"worker_id": worker_id, "prompt_version": "local-conservative-graph-v3", "schema_version": "3"},
    }


def _map_python(rel: str, file_info: dict, lines: list[str], worker_id: str) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    unresolved: list[dict] = []
    tree = ast.parse("\n".join(lines) + "\n")
    symbol_by_ast: dict[ast.AST, str] = {}
    symbols_by_name: dict[str, str] = {}
    parents: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._symbol(node, "class")
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._symbol(node, "method" if parents else "function")
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                dep = dependency_node_id(alias.name)
                nodes.append(_dependency_node(alias.name, rel, node.lineno, file_info, worker_id))
                edges.append(_edge(file_node_id(rel), dep, "imports", rel, node.lineno, node.lineno, "direct_syntax", worker_id, file_info))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                dep = dependency_node_id(node.module)
                nodes.append(_dependency_node(node.module, rel, node.lineno, file_info, worker_id))
                edges.append(_edge(file_node_id(rel), dep, "imports", rel, node.lineno, node.lineno, "direct_syntax", worker_id, file_info))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            source = _nearest_symbol(node, symbol_by_ast) or file_node_id(rel)
            call_name = _python_call_name(node.func)
            if call_name:
                target = symbols_by_name.get(call_name.split(".")[-1])
                if target:
                    edges.append(_edge(source, target, "calls", rel, node.lineno, node.lineno, "direct_syntax", worker_id, file_info))
                else:
                    unresolved.append(
                        {
                            "source_node": source,
                            "reference_kind": "call",
                            "raw_reference": call_name,
                            "source_file": rel,
                            "source_line": node.lineno,
                            "candidate_targets": [],
                            "reason": "Target is not defined in this mapped shard or is dynamically dispatched.",
                            "resolution_hint": "Resolve through imports, receiver type, registry, or framework configuration.",
                        }
                    )
            env_name = _python_env_name(node)
            if env_name:
                env_id = env_node_id(env_name)
                nodes.append(_env_node(env_name, rel, node.lineno, file_info, worker_id))
                edges.append(_edge(source, env_id, "reads_env", rel, node.lineno, node.lineno, "direct_syntax", worker_id, file_info))
            self.generic_visit(node)

        def _symbol(self, node: ast.AST, kind: str) -> None:
            name = getattr(node, "name", "<module>")
            qualified = ".".join([*parents, name]) if parents else name
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start) or start)
            signature = _python_signature(node, qualified, kind)
            node_id = stable_node_id(language="python", file=rel, qualified_name=qualified, kind=kind, signature=signature)
            symbol_by_ast[node] = node_id
            symbols_by_name[name] = node_id
            nodes.append(_symbol_node(node_id, kind, name, qualified, "python", rel, start, end, signature, file_info, worker_id))
            edges.append(_edge(file_node_id(rel), node_id, "defines", rel, start, end, "direct_syntax", worker_id, file_info))

    Visitor().visit(tree)
    return {"nodes": nodes, "edges": edges, "unresolved_refs": unresolved}


def _map_javascript(rel: str, file_info: dict, lines: list[str], worker_id: str, language: str) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    unresolved: list[dict] = []
    symbols_by_name: dict[str, str] = {}
    current_symbol = file_node_id(rel)
    for index, line in enumerate(lines, start=1):
        import_match = _JS_IMPORT_RE.search(line)
        if import_match:
            dep_name = import_match.group(1) or import_match.group(2) or ""
            dep = dependency_node_id(dep_name)
            nodes.append(_dependency_node(dep_name, rel, index, file_info, worker_id))
            edges.append(_edge(file_node_id(rel), dep, "imports", rel, index, index, "direct_syntax", worker_id, file_info))
        route_match = _JS_ROUTE_RE.search(line)
        if route_match:
            method, route = route_match.groups()
            route_id = route_node_id(method, route)
            nodes.append(_route_node(route_id, method, route, rel, index, file_info, worker_id))
            edges.append(_edge(route_id, current_symbol, "route_to", rel, index, index, "direct_syntax", worker_id, file_info))
        def_match = _JS_DEF_RE.search(line)
        if def_match:
            name = def_match.group(1) or def_match.group(2) or "<anonymous>"
            kind = "class" if "class" in line else "function"
            node_id = stable_node_id(language=language, file=rel, qualified_name=name, kind=kind, signature=line.strip())
            symbols_by_name[name] = node_id
            current_symbol = node_id
            nodes.append(_symbol_node(node_id, kind, name, name, language, rel, index, _js_symbol_end(lines, index), line.strip(), file_info, worker_id))
            edges.append(_edge(file_node_id(rel), node_id, "defines", rel, index, index, "direct_syntax", worker_id, file_info))
        for call in _CALL_RE.findall(line):
            if call in {"if", "for", "while", "switch", "function", "return", "catch"}:
                continue
            target = symbols_by_name.get(call)
            if target and target != current_symbol:
                edges.append(_edge(current_symbol, target, "calls", rel, index, index, "direct_syntax", worker_id, file_info))
            elif call not in symbols_by_name:
                unresolved.append(
                    {
                        "source_node": current_symbol,
                        "reference_kind": "call",
                        "raw_reference": call,
                        "source_file": rel,
                        "source_line": index,
                        "candidate_targets": [],
                        "reason": "Target is imported, framework-provided, or dynamically resolved.",
                        "resolution_hint": "Resolve with import/export and receiver context.",
                    }
                )
    return {"nodes": nodes, "edges": edges, "unresolved_refs": unresolved}


def _map_json_config(rel: str, file_info: dict, lines: list[str], worker_id: str) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    try:
        payload = json.loads("\n".join(lines) or "{}")
    except json.JSONDecodeError:
        return {"nodes": nodes, "edges": edges}
    if isinstance(payload, dict):
        for key in list(payload)[:200]:
            config_id = config_node_id(str(key))
            nodes.append(_config_node(str(key), rel, 1, file_info, worker_id))
            edges.append(_edge(file_node_id(rel), config_id, "defines", rel, 1, 1, "direct_syntax", worker_id, file_info))
    return {"nodes": nodes, "edges": edges}


def _map_sql(rel: str, file_info: dict, lines: list[str], worker_id: str) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    for index, line in enumerate(lines, start=1):
        for table in _SQL_TABLE_RE.findall(line):
            table_id = table_node_id(table)
            nodes.append(_table_node(table, rel, index, file_info, worker_id))
            edges.append(_edge(file_node_id(rel), table_id, "references", rel, index, index, "direct_syntax", worker_id, file_info))
    return {"nodes": nodes, "edges": edges}


def _symbol_node(
    node_id: str,
    kind: str,
    name: str,
    qualified: str,
    language: str,
    rel: str,
    start: int,
    end: int,
    signature: str,
    file_info: dict,
    worker_id: str,
) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "qualified_name": qualified,
        "language": language,
        "file": rel,
        "span": {"start_line": start, "end_line": max(start, end)},
        "signature": signature,
        "visibility": "internal",
        "content_hash": file_info.get("content_hash") or "",
        "attributes": risk_tags_for_path(rel, qualified),
        "evidence": [_evidence(rel, start, max(start, end), "direct_syntax", file_info)],
        "generated_by": {"worker_id": worker_id, "prompt_version": "local-conservative-graph-v3", "schema_version": "3"},
    }


def _dependency_node(name: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return _external_node(dependency_node_id(name), "dependency", name, rel, line, file_info, worker_id)


def _env_node(name: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return _external_node(env_node_id(name), "env_var", name, rel, line, file_info, worker_id)


def _config_node(name: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return _external_node(config_node_id(name), "config_key", name, rel, line, file_info, worker_id)


def _table_node(name: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return _external_node(table_node_id(name), "database_table", name, rel, line, file_info, worker_id)


def _route_node(node_id: str, method: str, route: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return {
        **_external_node(node_id, "http_route", f"{method.upper()} {route}", rel, line, file_info, worker_id),
        "method": method.upper(),
        "route": route,
    }


def _external_node(node_id: str, kind: str, name: str, rel: str, line: int, file_info: dict, worker_id: str) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "qualified_name": name,
        "language": language_for_path(rel),
        "file": rel,
        "span": {"start_line": line, "end_line": line},
        "signature": name,
        "visibility": "external" if kind == "dependency" else "repository",
        "content_hash": file_info.get("content_hash") or "",
        "attributes": [kind],
        "evidence": [_evidence(rel, line, line, "direct_syntax", file_info)],
        "generated_by": {"worker_id": worker_id, "prompt_version": "local-conservative-graph-v3", "schema_version": "3"},
    }


def _edge(source: str, target: str, edge_type: str, rel: str, start: int, end: int, evidence_kind: str, worker_id: str, file_info: dict) -> dict:
    evidence = [_evidence(rel, start, end, evidence_kind, file_info)]
    return {
        "id": stable_edge_id(source, target, edge_type, evidence),
        "from": source,
        "to": target,
        "type": edge_type,
        "status": "resolved",
        "evidence_kind": evidence_kind,
        "confidence": confidence_for_evidence(evidence_kind),
        "evidence": evidence,
        "generated_by": {"worker_id": worker_id, "prompt_version": "local-conservative-graph-v3"},
        "verification": {"independent_mappers": 1, "audited": False},
    }


def _evidence(rel: str, start: int, end: int, evidence_kind: str, file_info: dict) -> dict:
    return {
        "file": rel,
        "start_line": max(1, int(start or 1)),
        "end_line": max(max(1, int(start or 1)), int(end or start or 1)),
        "evidence_kind": evidence_kind,
        "content_hash": file_info.get("content_hash") or "",
    }


def _fallback_file_result(rel: str, file_info: dict, worker_id: str, reason: str) -> dict:
    node = _file_node(rel, file_info, language_for_path(rel), worker_id)
    node["warnings"] = [reason]
    return {"nodes": [node], "edges": [], "unresolved_refs": []}


def _is_test_file(rel: str) -> bool:
    lower = rel.lower()
    return "/test" in lower or "/tests" in lower or lower.endswith((".test.py", ".spec.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))


def _python_signature(node: ast.AST, qualified: str, kind: str) -> str:
    if kind == "class":
        return f"class {qualified}"
    args = getattr(node, "args", None)
    if not args:
        return qualified
    names = [arg.arg for arg in getattr(args, "args", [])]
    return f"{qualified}({', '.join(names)})"


def _python_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _python_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _python_env_name(node: ast.Call) -> str:
    name = _python_call_name(node.func)
    if name not in {"os.getenv", "getenv", "os.environ.get"}:
        return ""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return ""


def _nearest_symbol(node: ast.AST, symbols: dict[ast.AST, str]) -> str:
    best = ""
    best_start = -1
    line = int(getattr(node, "lineno", 0) or 0)
    for symbol_node, symbol_id in symbols.items():
        start = int(getattr(symbol_node, "lineno", 0) or 0)
        end = int(getattr(symbol_node, "end_lineno", start) or start)
        if start <= line <= end and start >= best_start:
            best = symbol_id
            best_start = start
    return best


def _js_symbol_end(lines: list[str], start: int) -> int:
    depth = 0
    seen_brace = False
    for index in range(start, min(len(lines), start + 200) + 1):
        line = lines[index - 1]
        depth += line.count("{") - line.count("}")
        seen_brace = seen_brace or "{" in line
        if seen_brace and depth <= 0:
            return index
    return start
