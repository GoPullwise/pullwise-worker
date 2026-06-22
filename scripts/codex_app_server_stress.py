from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codereview.app_server_runner import run_codex_app_server_turn
from codereview.config import CodexConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress one persistent Codex app-server with concurrent turns.")
    parser.add_argument("--repo", default=".", help="Repository/cwd for turns.")
    parser.add_argument("--jobs", type=int, default=6, help="Concurrent turns to start.")
    parser.add_argument("--timeout", type=int, default=600, help="Per-turn timeout in seconds.")
    parser.add_argument("--duration-seconds", type=int, default=0, help="Run repeated rounds for at least this many seconds.")
    parser.add_argument("--round-interval-seconds", type=int, default=0, help="Minimum seconds between round starts.")
    parser.add_argument("--stop-on-failure", action="store_true", help="Stop the sustained run after the first failed round.")
    parser.add_argument("--sqlite-home", default=os.environ.get("PULLWISE_CODEX_SQLITE_HOME") or "")
    parser.add_argument("--codex-command", default=os.environ.get("PULLWISE_CODEX_COMMAND") or "codex")
    parser.add_argument("--model", default=os.environ.get("PULLWISE_CODEX_MODEL") or "")
    parser.add_argument("--reasoning-effort", default=os.environ.get("PULLWISE_CODEX_REASONING_EFFORT") or "medium")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    run_root = Path(tempfile.mkdtemp(prefix="pullwise-app-server-stress-"))
    sqlite_home = Path(args.sqlite_home).resolve() if args.sqlite_home else run_root / "sqlite"
    sqlite_home.mkdir(parents=True, exist_ok=True)
    schema = run_root / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["round", "job", "ok"],
                "properties": {"round": {"type": "integer"}, "job": {"type": "integer"}, "ok": {"type": "boolean"}},
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CODEX_SQLITE_HOME"] = str(sqlite_home)
    config = CodexConfig(
        command=args.codex_command,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        env=env,
    )
    started = time.monotonic()
    jobs = max(1, int(args.jobs or 1))
    duration_seconds = max(0, int(args.duration_seconds or 0))
    deadline = started + duration_seconds if duration_seconds else 0
    round_index = 0
    results = []
    while True:
        round_started = time.monotonic()
        round_results = run_round(round_index, jobs, repo, run_root, schema, config, args.timeout)
        results.extend(round_results)
        round_payload = {
            "type": "round",
            "round": round_index,
            "jobs": jobs,
            "ok": sum(1 for item in round_results if item.get("ok")),
            "failed": sum(1 for item in round_results if not item.get("ok")),
            "failed_details": [
                {
                    "job": item.get("job"),
                    "returncode": item.get("returncode"),
                    "stderr": item.get("stderr"),
                    "output": item.get("output"),
                }
                for item in round_results
                if not item.get("ok")
            ][:10],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "round_duration_ms": int((time.monotonic() - round_started) * 1000),
            "run_root": str(run_root),
            "sqlite_home": str(sqlite_home),
        }
        print(json.dumps(round_payload, ensure_ascii=False, sort_keys=True), flush=True)
        round_index += 1
        if args.stop_on_failure and round_payload["failed"]:
            break
        if not duration_seconds or time.monotonic() >= deadline:
            break
        sleep_until = round_started + max(0, int(args.round_interval_seconds or 0))
        delay = min(sleep_until - time.monotonic(), deadline - time.monotonic())
        if delay > 0:
            time.sleep(delay)
    payload = {
        "backend": "app-server",
        "duration_seconds_requested": duration_seconds,
        "jobs_per_round": jobs,
        "rounds": round_index,
        "total_turns": len(results),
        "ok": sum(1 for item in results if item.get("ok")),
        "failed": [item for item in results if not item.get("ok")][:50],
        "failed_count": sum(1 for item in results if not item.get("ok")),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "run_root": str(run_root),
        "sqlite_home": str(sqlite_home),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["failed_count"] == 0 else 1


def run_round(
    round_index: int,
    jobs: int,
    repo: Path,
    run_root: Path,
    schema: Path,
    config: CodexConfig,
    timeout: int,
) -> list[dict]:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(run_one, round_index, index, repo, run_root, schema, config, timeout)
            for index in range(jobs)
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return results


def run_one(
    round_index: int,
    index: int,
    repo: Path,
    run_root: Path,
    schema: Path,
    config: CodexConfig,
    timeout: int,
) -> dict:
    output = run_root / f"round-{round_index}-job-{index}.json"
    events = run_root / f"round-{round_index}-job-{index}.events.jsonl"
    prompt = (
        "Return JSON only matching the schema with "
        f"round={round_index}, job={index}, and ok=true. Do not modify files."
    )
    result = run_codex_app_server_turn(
        cd=repo,
        prompt=prompt,
        output_schema=schema,
        output_file=output,
        sandbox="read-only",
        timeout_seconds=timeout,
        config=config,
        env=config.env,
        events_file=events,
    )
    parsed = {}
    if output.is_file():
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = {}
    return {
        "round": round_index,
        "job": index,
        "ok": (
            result.returncode == 0
            and parsed.get("ok") is True
            and parsed.get("round") == round_index
            and parsed.get("job") == index
        ),
        "returncode": result.returncode,
        "stderr": result.stderr[-500:],
        "output": parsed,
        "events": str(events),
    }


if __name__ == "__main__":
    raise SystemExit(main())
