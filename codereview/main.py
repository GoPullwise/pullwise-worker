from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .candidates.dedupe import dedupe_candidates
from .candidates.normalize import normalize_candidates
from .candidates.score import score_candidates
from .candidates.select import select_for_repro
from .codegraph_adapter import codegraph_affected_tests, preflight_codegraph
from .config import load_config
from .diff.analyzer import analyze_git_diff
from .diff.rough_symbols import map_rough_symbols
from .finder.runner import run_finders_parallel
from .finder.tasks import plan_finder_tasks
from .judge.runner import run_judges_parallel
from .repo import inspect_repo
from .report.render import collect_confirmed, collect_rejected, render_debug_report, render_final_report
from .repro.runner import run_repro_workers_parallel
from .slicing.context_pack import write_slices
from .slicing.planner import build_slices_with_codegraph
from .templates import ensure_project_files
from .utils.jsonl import write_json, write_jsonl


def run_review(checkout: Path, base_ref: str, head_ref: str, mode: str = "") -> Path:
    checkout = checkout.resolve(strict=False)
    ensure_project_files(checkout)
    config = load_config(checkout, mode=mode)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run = checkout / ".codereview" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    write_json(run / "meta.json", {"run_id": run_id, "mode": config.mode, "base": base_ref, "head": head_ref})

    repo_state = inspect_repo(checkout, base_ref, head_ref)
    write_json(run / "repo_state.json", repo_state)
    preflight_codegraph(checkout, run, config.codegraph)

    diff = analyze_git_diff(checkout, base_ref, head_ref)
    write_json(run / "diff" / "changed_files.json", diff.changed_files)
    write_json(run / "diff" / "hunks.json", diff.hunks)

    rough_symbols = map_rough_symbols(checkout, diff)
    write_json(run / "diff" / "rough_symbols.json", rough_symbols)

    affected_tests = codegraph_affected_tests(checkout, run, diff.changed_files, config.codegraph)
    slices = build_slices_with_codegraph(
        checkout=checkout,
        run=run,
        rough_symbols=rough_symbols,
        affected_tests=affected_tests,
        config=config,
    )
    write_slices(run / "slices", slices)

    finder_tasks = plan_finder_tasks(slices)
    raw_candidates = run_finders_parallel(checkout, run, finder_tasks, config)
    write_jsonl(run / "candidates" / "raw.jsonl", raw_candidates)

    normalized = normalize_candidates(raw_candidates)
    deduped = dedupe_candidates(normalized)
    scored = score_candidates(deduped)
    selected = select_for_repro(scored, config)
    write_jsonl(run / "candidates" / "normalized.jsonl", normalized)
    write_jsonl(run / "candidates" / "selected_for_repro.jsonl", selected)

    repro_results = run_repro_workers_parallel(checkout, run, selected, config)
    write_jsonl(run / "repro" / "results.jsonl", repro_results)

    judge_results = run_judges_parallel(run, selected, repro_results, checkout, config)
    write_jsonl(run / "judge" / "results.jsonl", judge_results)

    confirmed = collect_confirmed(selected, repro_results, judge_results)
    rejected = collect_rejected(selected, repro_results, judge_results)
    write_json(run / "reports" / "confirmed.json", confirmed)
    write_json(run / "reports" / "rejected.json", rejected)
    write_json(run / "reports" / "final.json", {"confirmed": confirmed})
    (run / "reports").mkdir(parents=True, exist_ok=True)
    (run / "reports" / "final.md").write_text(
        render_final_report(confirmed, rejected, base_ref=base_ref, head_ref=head_ref, run_id=run_id, mode=config.mode),
        encoding="utf-8",
    )
    (run / "reports" / "debug.md").write_text(
        render_debug_report(diff, slices, raw_candidates, selected, repro_results, judge_results),
        encoding="utf-8",
    )
    return run / "reports" / "final.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m codereview")
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--checkout", default=".")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--checkout", default=".")
    run_parser.add_argument("--base", required=True)
    run_parser.add_argument("--head", default="HEAD")
    run_parser.add_argument("--mode", choices=["fast", "standard", "deep"], default="")
    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    if args.command == "init":
        ensure_project_files(checkout)
        print(checkout / ".codereview")
        return 0
    try:
        final = run_review(checkout, args.base, args.head, mode=args.mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        message = str(exc)
        if "CodeGraph" in message or "codegraph" in message:
            print(message, file=sys.stderr)
            return 3
        print(message, file=sys.stderr)
        return 5
    print(final)
    confirmed = final.with_name("confirmed.json")
    return 1 if confirmed.is_file() and '"candidate"' in confirmed.read_text(encoding="utf-8") else 0


if __name__ == "__main__":
    raise SystemExit(main())
