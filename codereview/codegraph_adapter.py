from __future__ import annotations

import json
import os
from pathlib import Path

from .config import CodeGraphConfig
from .utils.jsonl import write_json
from .utils.process import ProcessResult, run_process


class CodeGraphError(RuntimeError):
    pass


def _codegraph_env(checkout: Path) -> dict[str, str]:
    del checkout
    env = os.environ.copy()
    env.pop("CODEGRAPH_DIR", None)
    return env


def _run_codegraph(checkout: Path, run: Path, config: CodeGraphConfig, args: list[str], name: str) -> ProcessResult:
    result = run_process(
        [config.command, *args],
        cwd=checkout,
        env=_codegraph_env(checkout),
        timeout=config.timeout_seconds,
    )
    write_json(run / "codegraph" / "raw" / f"{name}.json", result.to_dict())
    return result


def preflight_codegraph(checkout: Path, run: Path, config: CodeGraphConfig) -> dict:
    codegraph_dir = checkout / ".codegraph"
    status = _run_codegraph(checkout, run, config, ["status", str(checkout)], "status")
    if status.returncode != 0:
        init = _run_codegraph(checkout, run, config, ["init", str(checkout), "--index"], "init")
        if init.returncode != 0:
            write_json(
                run / "codegraph" / "preflight.json",
                {"ok": False, "status": status.to_dict(), "init": init.to_dict()},
            )
            raise CodeGraphError("CodeGraph preflight failed")
        status = _run_codegraph(checkout, run, config, ["status", str(checkout)], "status-after-init")
    reindex = None
    if config.reindex:
        reindex = _run_codegraph(checkout, run, config, ["index", str(checkout), "--force"], "index-force")
        if reindex.returncode == 0:
            status = _run_codegraph(checkout, run, config, ["status", str(checkout)], "status-after-index")
    sync = None
    if config.optional_sync:
        sync = _run_codegraph(checkout, run, config, ["sync", str(checkout)], "sync")
    ok = status.returncode == 0 and (reindex is None or reindex.returncode == 0) and (sync is None or sync.returncode == 0)
    payload = {
        "ok": ok,
        "status": status.to_dict(),
        "reindex": reindex.to_dict() if reindex else None,
        "sync": sync.to_dict() if sync else None,
        "codegraph_dir": str(codegraph_dir),
    }
    write_json(run / "codegraph" / "preflight.json", payload)
    if not ok:
        raise CodeGraphError("CodeGraph preflight failed")
    return payload


def codegraph_query(checkout: Path, run: Path, config: CodeGraphConfig, query: str, name: str) -> dict:
    commands = [["query", query, "--json"], ["context", query]]
    attempts = []
    for index, args in enumerate(commands):
        result = _run_codegraph(checkout, run, config, args, f"{name}-{index}")
        attempts.append(result.to_dict())
        if result.returncode != 0:
            continue
        text = (result.stdout or "").strip()
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"text": text}
        return {"query": query, "command": args, "result": parsed, "attempts": attempts}
    return {"query": query, "result": {}, "attempts": attempts}


def codegraph_symbol_context(checkout: Path, run: Path, config: CodeGraphConfig, symbol: str, file_path: str, name: str) -> dict:
    query = f"{symbol} {file_path}".strip()
    result = {"query": codegraph_query(checkout, run, config, query, f"{name}-query")}
    for command_name, args in {
        "callers": ["callers", symbol, "--json"],
        "callees": ["callees", symbol, "--json"],
        "impact": ["impact", symbol, "--depth", "2", "--json"],
    }.items():
        if not symbol or symbol == "<module>":
            continue
        process = _run_codegraph(checkout, run, config, args, f"{name}-{command_name}")
        try:
            parsed = json.loads(process.stdout) if process.stdout.strip() else {}
        except json.JSONDecodeError:
            parsed = {"text": process.stdout}
        result[command_name] = {"command": args, "process": process.to_dict(), "result": parsed}
    return result


def codegraph_affected_tests(checkout: Path, run: Path, changed_files: list[str], config: CodeGraphConfig) -> list[dict]:
    if not changed_files:
        write_json(run / "codegraph" / "affected_tests.json", [])
        return []
    process = _run_codegraph(checkout, run, config, ["affected", *changed_files[:80], "--json"], "affected-tests")
    try:
        parsed = json.loads(process.stdout) if process.stdout.strip() else []
    except json.JSONDecodeError:
        parsed = {"text": process.stdout}
    tests = [{"command": process.command, "result": parsed, "returncode": process.returncode}]
    write_json(run / "codegraph" / "affected_tests.json", tests)
    return tests
