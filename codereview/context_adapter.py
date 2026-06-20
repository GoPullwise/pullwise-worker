from __future__ import annotations

import json
from pathlib import Path

from .codex_runner import base_env, run_codex_exec
from .config import ReviewConfig
from .utils.jsonl import write_json
from .utils.paths import safe_relative_path
from .utils.process import ProcessResult


def preflight_context(checkout: Path, run: Path, config: ReviewConfig) -> dict:
    payload = {
        "ok": True,
        "source": "codex_repository_context" if config.context.enabled else "repository_static",
        "context_dir": str(run / "context"),
    }
    write_json(run / "context" / "preflight.json", payload)
    return payload


def symbol_context(
    checkout: Path,
    run: Path,
    config: ReviewConfig,
    symbol: str,
    file_path: str,
    line: int,
    name: str,
) -> dict:
    seed = build_static_seed(checkout, symbol=symbol, file_path=file_path, line=line)
    write_json(run / "context" / "seed" / f"{name}.json", seed)
    if not config.context.enabled:
        return wrap_context(seed, static_payload(seed, reason="context generation disabled"), process=None)

    output = run / "context" / f"{name}.result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    process = run_codex_exec(
        cd=checkout,
        prompt=context_prompt(seed),
        output_schema=checkout / ".codereview" / "schemas" / "context_result.schema.json",
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.context.timeout_seconds,
        config=config.codex,
        env=base_env(checkout, config.codex),
    )
    write_json(run / "context" / "raw" / f"{name}.json", process.to_dict())
    generated, parse_error = read_context_output(output)
    if process.returncode != 0 or generated is None:
        reason = parse_error or f"codex context generation exited {process.returncode}"
        fallback = static_payload(seed, reason=reason)
        return wrap_context(seed, fallback, process=process)
    return wrap_context(seed, sanitize_generated_context(checkout, seed, generated), process=process)


def build_static_seed(checkout: Path, *, symbol: str, file_path: str, line: int) -> dict:
    rel = safe_relative_path(file_path) or str(file_path or "").strip()
    path = checkout / rel
    lines: list[str] = []
    if rel and path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end, snippet = snippet_window(lines, line or 1)
    return {
        "symbol": str(symbol or "<module>"),
        "file": rel,
        "line": max(1, int(line or 1)),
        "snippet_start": start,
        "snippet_end": end,
        "snippet": snippet,
    }


def snippet_window(lines: list[str], line: int, *, radius: int = 40, max_chars: int = 12000) -> tuple[int, int, list[str]]:
    if not lines:
        return 1, 1, []
    center = max(1, min(len(lines), int(line or 1)))
    start = max(1, center - radius)
    end = min(len(lines), center + radius)
    rendered = [f"{number}: {lines[number - 1]}" for number in range(start, end + 1)]
    while rendered and sum(len(item) + 1 for item in rendered) > max_chars:
        if len(rendered) <= 1:
            rendered[0] = rendered[0][:max_chars]
            break
        rendered.pop()
    return start, start + len(rendered) - 1, rendered


def context_prompt(seed: dict) -> str:
    return "\n".join(
        [
            "You are generating repository context for a code review slice.",
            "Return JSON only matching context_result.schema.json.",
            "Use the repository in the current directory. Read files as needed, but do not modify files and do not use the network.",
            "Identify the focal symbol, likely direct callers, likely direct callees, impact radius, and repository-relative files that matter.",
            "Prefer concrete file paths and line numbers over prose. Keep the result compact.",
            "",
            "Seed context:",
            "```json",
            json.dumps(seed, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def read_context_output(path: Path) -> tuple[dict | None, str]:
    if not path.is_file():
        return None, "codex context generation did not produce an output file"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"codex context generation produced invalid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "codex context generation produced non-object JSON"
    return value, ""


def sanitize_generated_context(checkout: Path, seed: dict, generated: dict) -> dict:
    fallback = static_payload(seed, reason="")
    files = safe_existing_files(checkout, generated.get("files"))
    if not files:
        files = fallback["files"]
    path_summary = string_list(generated.get("path_summary"), limit=24)
    if not path_summary:
        path_summary = string_list(generated.get("summary"), limit=12) or fallback["path_summary"]
    return {
        "summary": string_list(generated.get("summary"), limit=12) or fallback["summary"],
        "files": files,
        "path_summary": path_summary,
        "nodes": object_list(generated.get("nodes"), limit=40) or fallback["nodes"],
        "edges": object_list(generated.get("edges"), limit=80),
        "callers": object_list(generated.get("callers"), limit=40),
        "callees": object_list(generated.get("callees"), limit=40),
        "impact": object_list(generated.get("impact"), limit=40) or fallback["impact"],
    }


def static_payload(seed: dict, *, reason: str) -> dict:
    file_path = str(seed.get("file") or "")
    symbol = str(seed.get("symbol") or "<module>")
    line = int(seed.get("line") or 1)
    summary = [f"{symbol} in {file_path}:{line}"] if file_path else [symbol]
    path_summary = [f"{file_path}:{line} {symbol}".strip()]
    payload = {
        "summary": summary,
        "files": [file_path] if file_path else [],
        "path_summary": path_summary,
        "nodes": [
            {
                "name": symbol,
                "file": file_path,
                "line": line,
                "kind": "symbol",
            }
        ],
        "edges": [],
        "callers": [],
        "callees": [],
        "impact": [{"file": file_path, "line": line, "reason": "focal slice"}] if file_path else [],
    }
    if reason:
        payload["fallback_reason"] = reason
    return payload


def wrap_context(seed: dict, payload: dict, *, process: ProcessResult | None) -> dict:
    symbol = str(seed.get("symbol") or "<module>")
    file_path = str(seed.get("file") or "")
    query = f"{symbol} {file_path}".strip()
    process_dict = process.to_dict() if process is not None else {}
    source = "codex" if process is not None and process.returncode == 0 and not payload.get("fallback_reason") else "repository_static"
    return {
        "source": source,
        "query": {
            "query": query,
            "command": ["codex", "exec", "repository-context"],
            "result": payload,
            "attempts": [process_dict] if process_dict else [],
        },
        "callers": {
            "command": ["codex", "context", "callers", symbol],
            "process": process_dict,
            "result": {"items": payload.get("callers") or []},
        },
        "callees": {
            "command": ["codex", "context", "callees", symbol],
            "process": process_dict,
            "result": {"items": payload.get("callees") or []},
        },
        "impact": {
            "command": ["codex", "context", "impact", symbol],
            "process": process_dict,
            "result": {"items": payload.get("impact") or []},
        },
        "files": payload.get("files") or [],
        "path_summary": payload.get("path_summary") or [],
    }


def safe_existing_files(checkout: Path, value: object) -> list[str]:
    files: list[str] = []
    candidates = value if isinstance(value, list) else []
    for item in candidates:
        rel = safe_relative_path(item)
        if rel and (checkout / rel).is_file() and rel not in files:
            files.append(rel)
        if len(files) >= 40:
            break
    return files


def string_list(value: object, *, limit: int) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, (str, int, float))]
    else:
        items = []
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            result.append(text[:500])
        if len(result) >= limit:
            break
    return result


def object_list(value: object, *, limit: int) -> list[object]:
    if not isinstance(value, list):
        return []
    return value[:limit]
