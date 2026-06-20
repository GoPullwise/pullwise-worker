from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .candidates.dedupe import dedupe_candidates
from .candidates.normalize import normalize_candidates
from .candidates.score import score_candidates
from .candidates.select import select_for_repro
from .context_adapter import preflight_context
from .config import load_config
from .finder.runner import run_finders_parallel
from .finder.tasks import plan_finder_tasks
from .judge.runner import run_judges_parallel
from .repo import inspect_repo
from .repository.snapshot import analyze_repository_snapshot
from .repository.symbols import map_repository_symbols
from .report.render import collect_confirmed, collect_rejected, render_debug_report, render_final_report
from .repro.runner import run_repro_workers_parallel
from .slicing.context_pack import write_slices
from .slicing.planner import build_slices_with_context
from .templates import ensure_project_files
from .utils.jsonl import write_json, write_jsonl


def run_review(checkout: Path, head_ref: str, mode: str = "") -> Path:
    checkout = checkout.resolve(strict=False)
    ensure_project_files(checkout)
    config = load_config(checkout, mode=mode)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run = checkout / ".codereview" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    write_json(run / "meta.json", {"run_id": run_id, "mode": config.mode, "scope": "repository", "head": head_ref})

    repo_state = inspect_repo(checkout, head_ref)
    write_json(run / "repo_state.json", repo_state)
    preflight = preflight_context(checkout, run, config)

    snapshot = analyze_repository_snapshot(checkout, head_ref)
    write_json(run / "repository" / "files.json", snapshot.files)
    write_json(run / "repository" / "spans.json", snapshot.spans)

    repository_symbols = map_repository_symbols(checkout, snapshot)
    write_json(run / "repository" / "symbols.json", repository_symbols)

    slices = build_slices_with_context(
        checkout=checkout,
        run=run,
        rough_symbols=repository_symbols,
        repository_tests=[],
        config=config,
    )
    write_slices(run / "slices", slices)

    finder_tasks = plan_finder_tasks(slices)
    raw_candidates = run_finders_parallel(checkout, run, finder_tasks, config)
    write_jsonl(run / "candidates" / "raw.jsonl", raw_candidates)

    normalized = normalize_candidates(raw_candidates, checkout=checkout, run=run)
    deduped = dedupe_candidates(normalized)
    scored = score_candidates(deduped)
    selected = select_for_repro(scored, config)
    write_jsonl(run / "candidates" / "normalized.jsonl", normalized)
    write_json(run / "candidates" / "deduped.json", deduped)
    write_json(run / "candidates" / "scored.json", scored)
    write_jsonl(run / "candidates" / "selected_for_repro.jsonl", selected)

    repro_results = run_repro_workers_parallel(checkout, run, selected, config)
    write_jsonl(run / "repro" / "results.jsonl", repro_results)

    judge_results = run_judges_parallel(run, selected, repro_results, checkout, config)
    write_jsonl(run / "judge" / "results.jsonl", judge_results)

    confirmed = collect_confirmed(selected, repro_results, judge_results)
    rejected = collect_rejected(selected, repro_results, judge_results)
    pipeline_summary = build_pipeline_summary(
        preflight=preflight,
        snapshot=snapshot,
        slices=slices,
        finder_tasks=finder_tasks,
        raw_candidates=raw_candidates,
        normalized=normalized,
        deduped=deduped,
        scored=scored,
        selected=selected,
        repro_results=repro_results,
        judge_results=judge_results,
        confirmed=confirmed,
        rejected=rejected,
    )
    write_json(run / "reports" / "confirmed.json", confirmed)
    write_json(run / "reports" / "rejected.json", rejected)
    write_json(run / "reports" / "final.json", {"confirmed": confirmed})
    write_json(run / "reports" / "summary.json", pipeline_summary)
    (run / "reports").mkdir(parents=True, exist_ok=True)
    (run / "reports" / "final.md").write_text(
        render_final_report(
            confirmed,
            rejected,
            blocked=pipeline_summary["reports"]["blocked"],
            head_ref=head_ref,
            run_id=run_id,
            mode=config.mode,
        ),
        encoding="utf-8",
    )
    (run / "reports" / "debug.md").write_text(
        render_debug_report(snapshot, slices, raw_candidates, selected, repro_results, judge_results, pipeline_summary),
        encoding="utf-8",
    )
    return run / "reports" / "final.md"


def build_pipeline_summary(
    *,
    preflight: dict,
    snapshot: object,
    slices: list[dict],
    finder_tasks: list[object],
    raw_candidates: list[dict],
    normalized: list[dict],
    deduped: list[dict],
    scored: list[dict],
    selected: list[dict],
    repro_results: list[dict],
    judge_results: list[dict],
    confirmed: list[dict],
    rejected: list[dict],
) -> dict:
    finder_blocked = [blocked_summary(item) for item in raw_candidates if item.get("status") == "blocked"]
    repro_blocked = [
        blocked_summary(item)
        for item in repro_results
        if item.get("status") == "blocked" or _nested_status(item, "result") == "blocked"
    ]
    judge_confirmed = [item for item in judge_results if item.get("status") == "confirmed"]
    judge_rejected = [item for item in judge_results if item.get("status") == "rejected"]
    judge_blocked = [item for item in judge_results if item.get("status") == "blocked"]
    return {
        "preflight": {
            "ok": preflight.get("ok") is True,
            "contextSource": preflight.get("source"),
            "contextDir": preflight.get("context_dir"),
        },
        "repository": {
            "files": len(getattr(snapshot, "files", []) or []),
            "spans": len(getattr(snapshot, "spans", []) or []),
        },
        "slices": {
            "count": len(slices),
            "ids": [str(item.get("slice_id") or "") for item in slices if isinstance(item, dict)],
        },
        "finder": {
            "tasks": len(finder_tasks),
            "results": len(raw_candidates),
            "blocked": len(finder_blocked),
            "blockedItems": finder_blocked[:20],
            "candidates": sum(len((item.get("result") or {}).get("candidates") or []) for item in raw_candidates if isinstance(item.get("result"), dict)),
        },
        "candidates": {
            "normalized": len(normalized),
            "valid": sum(1 for item in normalized if item.get("valid")),
            "deduped": len(deduped),
            "scored": len(scored),
            "selectedForRepro": len(selected),
            "selectedIds": [str(item.get("issue_id") or "") for item in selected],
        },
        "repro": {
            "results": len(repro_results),
            "reproduced": sum(1 for item in repro_results if _nested_status(item, "result") == "reproduced"),
            "rejected": sum(1 for item in repro_results if _nested_status(item, "result") in {"not_reproduced", "rejected"}),
            "blocked": len(repro_blocked),
            "blockedItems": repro_blocked[:20],
        },
        "judge": {
            "confirmed": len(judge_confirmed),
            "rejected": len(judge_rejected),
            "blocked": len(judge_blocked),
            "confirmedIds": [str(item.get("candidate_id") or "") for item in judge_confirmed],
            "rejectedIds": [str(item.get("candidate_id") or "") for item in judge_rejected],
            "blockedIds": [str(item.get("candidate_id") or "") for item in judge_blocked],
        },
        "reports": {
            "confirmed": len(confirmed),
            "rejected": len(rejected),
            "blocked": len(finder_blocked) + len(repro_blocked) + len(judge_blocked),
        },
    }


def blocked_summary(item: dict) -> dict:
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    summary = {
        "candidateId": item.get("candidate_id") or result.get("candidate_id") or task.get("slice_id"),
        "focus": task.get("focus"),
        "sliceId": task.get("slice_id"),
        "reason": item.get("blocked_reason") or result.get("why_not_reproduced") or result.get("summary") or "",
    }
    process = blocked_process_summary(item.get("process"))
    if process:
        summary["process"] = process
    return summary


def blocked_process_summary(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    payload: dict[str, object] = {}
    for source_key, target_key in (
        ("returncode", "returncode"),
        ("timed_out", "timedOut"),
        ("duration_ms", "durationMs"),
        ("queueWaitMs", "queueWaitMs"),
        ("execDurationMs", "execDurationMs"),
        ("stdout_path", "stdoutPath"),
        ("stderr_path", "stderrPath"),
    ):
        if source_key in value:
            payload[target_key] = value.get(source_key)
    stdout_tail = compact_debug_text(value.get("stdout"), limit=1200)
    stderr_tail = compact_debug_text(value.get("stderr"), limit=1200)
    if stdout_tail:
        payload["stdoutTail"] = stdout_tail
    if stderr_tail:
        payload["stderrTail"] = stderr_tail
    command = value.get("command")
    if isinstance(command, list):
        payload["command"] = [compact_debug_text(part, limit=200) for part in command[:40]]
        if len(command) > 40:
            payload["commandTruncated"] = True
    return {key: val for key, val in payload.items() if val not in ("", None)}


def compact_debug_text(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[-limit:].lstrip()
    return text


def _nested_status(item: dict, key: str) -> str:
    value = item.get(key) if isinstance(item, dict) else {}
    return str(value.get("status") or "").lower() if isinstance(value, dict) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codereview")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--checkout", default=".")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--checkout", default=".")
    run_parser.add_argument("--head", default="HEAD")
    run_parser.add_argument("--mode", choices=["fast", "standard", "deep"], default="")
    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    if args.command == "init":
        ensure_project_files(checkout)
        print(checkout / ".codereview")
        return 0
    try:
        final = run_review(checkout, args.head, mode=args.mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        message = str(exc)
        print(message, file=sys.stderr)
        return 5
    print(final)
    confirmed = final.with_name("confirmed.json")
    if not confirmed.is_file():
        return 0
    try:
        payload = json.loads(confirmed.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 5
    return 1 if isinstance(payload, list) and payload else 0


if __name__ == "__main__":
    raise SystemExit(main())
