from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .candidates.dedupe import dedupe_candidates
from .candidates.normalize import normalize_candidates
from .candidates.score import score_candidates
from .candidates.select import select_for_repro
from .context_adapter import preflight_context
from .config import load_config
from .finder.runner import run_finders_parallel
from .finder.tasks import plan_finder_tasks
from .graph.audit import audit_graph, run_agent_graph_audit
from .graph.census import run_repository_census, validate_census_coverage
from .graph.index_sqlite import rebuild_sqlite_index
from .graph.link import apply_cross_shard_linking
from .graph.mapper import map_graph_tasks
from .graph.merge import merge_graph_results, normalize_graph_for_inventory, write_graph_artifacts
from .graph.repair import plan_repairs
from .graph.scheduler import plan_graph_tasks
from .inventory.git_inventory import build_git_inventory
from .judge.runner import run_judges_parallel
from .repo import inspect_repo
from .repository.snapshot import analyze_repository_snapshot
from .repository.symbols import map_repository_symbols
from .report.render import collect_confirmed, collect_rejected, render_debug_report, render_final_report
from .review.candidate_verifier import run_candidate_verifiers_parallel, select_reproducible_candidates
from .repro.runner import run_repro_workers_parallel
from .snapshot import capture_source_state, create_immutable_snapshot, source_state_changed
from .templates import ensure_project_files
from .units.context import write_review_units
from .units.coverage import build_unit_coverage, require_full_unit_coverage
from .units.planner import build_all_review_units
from .utils.jsonl import write_json, write_jsonl
from .utils.paths import safe_relative_path
from .utils.process import raise_if_cancelled_callback_exception


ProgressCallback = Callable[[dict], None]


def run_review(checkout: Path, mode: str = "", scan_mode: str = "", progress: ProgressCallback | None = None) -> Path:
    checkout = checkout.resolve(strict=False)
    ensure_project_files(checkout)
    config = load_config(checkout, mode=mode, scan_mode=scan_mode)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run = checkout / ".codereview" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    write_json(run / "meta.json", {"run_id": run_id, "mode": config.mode, "scan_mode": config.scan.mode, "scope": "full-repository"})
    _emit_progress(progress, "setup", "GraphVerified: preparing run", run_id=run_id)

    source_state_before = capture_source_state(checkout, include_untracked=config.scan.include_untracked)
    write_json(run / "source_state_before.json", source_state_before)
    repo_state = inspect_repo(checkout)
    write_json(run / "repo_state.json", repo_state)

    _emit_progress(progress, "inventory", "GraphVerified: building repository inventory", run_id=run_id)
    inventory = build_git_inventory(checkout, include_untracked=config.scan.include_untracked)
    write_json(run / "artifacts" / "inventory" / "inventory.json", inventory)

    _emit_progress(progress, "snapshot", "GraphVerified: creating immutable snapshot", run_id=run_id)
    snapshot_manifest = create_immutable_snapshot(checkout, inventory, run)
    write_json(run / "artifacts" / "inventory" / "snapshot.json", snapshot_manifest)
    snapshot_repo = Path(str(snapshot_manifest["snapshot_repo"]))
    snapshot_state_before = capture_source_state(snapshot_repo, include_untracked=True)
    write_json(run / "snapshot_source_state_before.json", snapshot_state_before)
    preflight = preflight_context(snapshot_repo, run, config)

    _emit_progress(progress, "census", "Graph: repository census", run_id=run_id)
    census = run_repository_census(snapshot_repo, run, inventory, config)
    census_errors = validate_census_coverage(census, inventory)
    write_json(run / "artifacts" / "graph" / "census.json", census)
    write_json(run / "artifacts" / "graph" / "census-validation.json", {"errors": census_errors})
    if census_errors:
        raise RuntimeError(f"repository census failed coverage validation: {'; '.join(census_errors)}")

    graph_tasks = plan_graph_tasks(census, inventory, config)
    write_jsonl(run / "artifacts" / "graph" / "tasks.jsonl", graph_tasks)
    _emit_progress(progress, "graph", f"Graph: mapping shards 0/{len(graph_tasks)}", current=0, total=len(graph_tasks), run_id=run_id)
    shard_results = _map_graph_tasks_with_progress(snapshot_repo, graph_tasks, inventory, config, run=run, progress=progress)
    write_jsonl(run / "artifacts" / "graph" / "shard-results.jsonl", shard_results)
    _emit_progress(progress, "graph", "Graph: merging shard results", run_id=run_id)
    graph = merge_graph_results(shard_results)
    graph = normalize_graph_for_inventory(apply_cross_shard_linking(snapshot_repo, run, graph, config), inventory, snapshot_repo)
    graph_dir = run / "artifacts" / "graph"
    _emit_progress(progress, "graph", "Graph: auditing evidence quality", run_id=run_id)
    audit = audit_graph(graph, inventory, snapshot_repo)
    repair_history: list[dict] = []
    for round_index in range(config.graph.max_repair_rounds):
        if audit.get("quality_gate_passed"):
            break
        repairs = plan_repairs(audit)
        repair_tasks = _graph_repair_tasks(repairs, start_index=len(graph_tasks) + len(repair_history) + 1)
        if not repair_tasks:
            break
        _emit_progress(
            progress,
            "graph",
            f"Graph: repair round {round_index + 1} 0/{len(repair_tasks)}",
            current=0,
            total=len(repair_tasks),
            run_id=run_id,
        )
        repair_results = _map_graph_tasks_with_progress(
            snapshot_repo,
            repair_tasks,
            inventory,
            config,
            run=run,
            progress=progress,
            progress_label=f"Graph: repair round {round_index + 1}",
        )
        repair_history.append({"round": round_index + 1, "tasks": repair_tasks, "results": repair_results})
        shard_results.extend(repair_results)
        graph = merge_graph_results(shard_results)
        graph = normalize_graph_for_inventory(apply_cross_shard_linking(snapshot_repo, run, graph, config), inventory, snapshot_repo)
        audit = audit_graph(graph, inventory, snapshot_repo)
    write_graph_artifacts(graph_dir, graph)
    if config.graph.use_sqlite_index:
        _emit_progress(progress, "graph", "Graph: rebuilding query index", run_id=run_id)
        rebuild_sqlite_index(graph, graph_dir / "graph.sqlite3")
    _emit_progress(progress, "graph", "Graph: agent audit", run_id=run_id)
    agent_audit = run_agent_graph_audit(snapshot_repo, run, graph, inventory, audit, config)
    write_json(graph_dir / "audit.json", audit)
    write_json(graph_dir / "agent-audit.json", agent_audit)
    write_json(graph_dir / "repair-history.json", repair_history)
    if not audit.get("quality_gate_passed"):
        _emit_progress(progress, "graph", _graph_quality_gate_failure_message(audit), run_id=run_id)
        raise RuntimeError(f"graph quality gate failed: {'; '.join(audit.get('quality_errors') or [])}")

    _emit_progress(progress, "repository", "Repository: analyzing snapshot", run_id=run_id)
    snapshot = analyze_repository_snapshot(snapshot_repo, inventory)
    write_json(run / "repository" / "files.json", snapshot.files)
    write_json(run / "repository" / "spans.json", snapshot.spans)

    _emit_progress(progress, "repository", "Repository: mapping symbols", run_id=run_id)
    repository_symbols = map_repository_symbols(snapshot_repo, snapshot)
    write_json(run / "repository" / "symbols.json", repository_symbols)

    _emit_progress(progress, "review_units", "Review units: planning coverage", run_id=run_id)
    review_units = build_all_review_units(graph, inventory, census, config)
    write_review_units(run, review_units)
    planned_coverage = build_unit_coverage(graph, inventory, review_units)
    planned_coverage["critical_unresolved_graph_edges"] = audit.get("critical_unresolved", 0)
    require_full_unit_coverage(planned_coverage)
    write_json(run / "artifacts" / "review-units" / "coverage-planned.json", planned_coverage)

    finder_tasks = plan_finder_tasks(review_units)
    _emit_progress(progress, "finder", f"Finder: review tasks 0/{len(finder_tasks)}", current=0, total=len(finder_tasks), run_id=run_id)
    raw_candidates = _run_finders_with_progress(snapshot_repo, run, finder_tasks, config, progress)
    context_repair_tasks = _finder_context_repair_tasks(raw_candidates, start_index=len(graph_tasks) + len(repair_history) + 1)
    context_repair_history: list[dict] = []
    if context_repair_tasks:
        _emit_progress(
            progress,
            "graph",
            f"Graph: context repair 0/{len(context_repair_tasks)}",
            current=0,
            total=len(context_repair_tasks),
            run_id=run_id,
        )
        context_repair_results = _map_graph_tasks_with_progress(
            snapshot_repo,
            context_repair_tasks,
            inventory,
            config,
            run=run,
            progress=progress,
            progress_label="Graph: context repair",
        )
        context_repair_history.append({"round": 1, "tasks": context_repair_tasks, "results": context_repair_results})
        shard_results.extend(context_repair_results)
        graph = merge_graph_results(shard_results)
        graph = normalize_graph_for_inventory(apply_cross_shard_linking(snapshot_repo, run, graph, config), inventory, snapshot_repo)
        audit = audit_graph(graph, inventory, snapshot_repo)
        write_graph_artifacts(graph_dir, graph)
        write_json(graph_dir / "audit.json", audit)
        write_json(graph_dir / "context-repair-history.json", context_repair_history)
        if not audit.get("quality_gate_passed"):
            _emit_progress(progress, "graph", _graph_quality_gate_failure_message(audit), run_id=run_id)
            raise RuntimeError(f"graph quality gate failed after context repair: {'; '.join(audit.get('quality_errors') or [])}")
        agent_audit = run_agent_graph_audit(snapshot_repo, run, graph, inventory, audit, config)
        write_json(graph_dir / "agent-audit.json", agent_audit)
        previous_finder_tasks = finder_tasks
        previous_raw_candidates = raw_candidates
        review_units = build_all_review_units(graph, inventory, census, config)
        write_review_units(run, review_units)
        planned_coverage = build_unit_coverage(graph, inventory, review_units)
        planned_coverage["critical_unresolved_graph_edges"] = audit.get("critical_unresolved", 0)
        require_full_unit_coverage(planned_coverage)
        write_json(run / "artifacts" / "review-units" / "coverage-planned.json", planned_coverage)
        finder_tasks = plan_finder_tasks(review_units)
        repair_finder_tasks, kept_raw_candidates = _finder_context_repair_rerun_plan(
            previous_raw_candidates,
            previous_finder_tasks,
            finder_tasks,
        )
        _emit_progress(
            progress,
            "finder",
            f"Finder: review tasks 0/{len(repair_finder_tasks)}",
            current=0,
            total=len(repair_finder_tasks),
            run_id=run_id,
        )
        repaired_raw_candidates = _run_finders_with_progress(snapshot_repo, run, repair_finder_tasks, config, progress) if repair_finder_tasks else []
        raw_candidates = [*kept_raw_candidates, *repaired_raw_candidates]
    write_jsonl(run / "candidates" / "raw.jsonl", raw_candidates)

    _emit_progress(progress, "candidates", "Candidates: normalizing and scoring", run_id=run_id)
    executed_coverage = build_unit_coverage(graph, inventory, review_units, raw_candidates)
    executed_coverage["critical_unresolved_graph_edges"] = audit.get("critical_unresolved", 0)
    require_full_unit_coverage(executed_coverage, require_baseline_review=config.units.require_baseline_for_every_unit)
    write_json(run / "artifacts" / "review-units" / "coverage-executed.json", executed_coverage)

    normalized = normalize_candidates(raw_candidates, checkout=snapshot_repo, run=run)
    deduped = dedupe_candidates(normalized)
    scored = score_candidates(deduped)
    selected_for_verification = select_for_repro(scored, config)
    _emit_progress(
        progress,
        "verification",
        f"Verification: candidates 0/{min(len(selected_for_verification), config.candidates.max_total_for_verification)}",
        current=0,
        total=min(len(selected_for_verification), config.candidates.max_total_for_verification),
        run_id=run_id,
    )
    verification_results = _run_candidate_verifiers_with_progress(
        selected_for_verification,
        graph,
        config,
        checkout=snapshot_repo,
        run=run,
        progress=progress,
    )
    selected = select_reproducible_candidates(selected_for_verification, verification_results, config)
    write_jsonl(run / "candidates" / "normalized.jsonl", normalized)
    write_json(run / "candidates" / "deduped.json", deduped)
    write_json(run / "candidates" / "scored.json", scored)
    write_jsonl(run / "candidates" / "selected_for_verification.jsonl", selected_for_verification)
    write_jsonl(run / "candidates" / "verification.jsonl", verification_results)
    write_jsonl(run / "candidates" / "selected_for_repro.jsonl", selected)

    _emit_progress(progress, "reproduction", f"Reproduction: candidates 0/{len(selected)}", current=0, total=len(selected), run_id=run_id)
    repro_results = _run_repro_workers_with_progress(snapshot_repo, run, selected, config, progress)
    write_jsonl(run / "repro" / "results.jsonl", repro_results)

    _emit_progress(progress, "judge", f"Judge: candidates 0/{len(repro_results)}", current=0, total=len(repro_results), run_id=run_id)
    judge_results = _run_judges_with_progress(run, selected, repro_results, snapshot_repo, config, progress)
    write_jsonl(run / "judge" / "results.jsonl", judge_results)

    snapshot_state_after = capture_source_state(snapshot_repo, include_untracked=True)
    stale_snapshot = source_state_changed(snapshot_state_before, snapshot_state_after)
    write_json(run / "snapshot_source_state_after.json", {**snapshot_state_after, "changed_during_scan": stale_snapshot})
    if stale_snapshot:
        raise RuntimeError("immutable snapshot changed during graph-verified read-only stages")

    _emit_progress(progress, "report", "Report: rendering confirmed findings", run_id=run_id)
    confirmed = collect_confirmed(selected, repro_results, judge_results)
    rejected = collect_rejected(selected, repro_results, judge_results)
    pipeline_summary = build_pipeline_summary(
        preflight=preflight,
        snapshot=snapshot,
        review_units=review_units,
        unit_coverage=executed_coverage,
        snapshot_manifest=snapshot_manifest,
        finder_tasks=finder_tasks,
        raw_candidates=raw_candidates,
        normalized=normalized,
        deduped=deduped,
        scored=scored,
        selected_for_verification=selected_for_verification,
        verification_results=verification_results,
        selected=selected,
        repro_results=repro_results,
        judge_results=judge_results,
        confirmed=confirmed,
        rejected=rejected,
        graph_audit=audit,
        agent_graph_audit=agent_audit,
    )
    write_json(run / "reports" / "confirmed.json", confirmed)
    write_json(run / "reports" / "rejected.json", rejected)
    write_json(run / "reports" / "final.json", {"confirmed": confirmed})
    write_json(run / "reports" / "summary.json", pipeline_summary)
    source_state_after = capture_source_state(checkout, include_untracked=config.scan.include_untracked)
    stale_source = source_state_changed(source_state_before, source_state_after)
    write_json(run / "source_state_after.json", {**source_state_after, "changed_during_scan": stale_source})
    if stale_source and config.scan.fail_on_source_change:
        raise RuntimeError("source checkout changed during full-repository scan")
    (run / "reports").mkdir(parents=True, exist_ok=True)
    (run / "reports" / "final.md").write_text(
        render_final_report(
            confirmed,
            rejected,
            blocked=pipeline_summary["reports"]["blocked"],
            run_id=run_id,
            mode=config.mode,
            graph_schema=f"v{config.graph.schema_version}",
            coverage=executed_coverage,
            snapshot=snapshot_manifest,
        ),
        encoding="utf-8",
    )
    (run / "reports" / "debug.md").write_text(
        render_debug_report(snapshot, review_units, raw_candidates, selected, repro_results, judge_results, pipeline_summary),
        encoding="utf-8",
    )
    return run / "reports" / "final.md"


def _emit_progress(
    progress: ProgressCallback | None,
    stage: str,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
    run_id: str = "",
) -> None:
    if progress is None:
        return
    payload: dict[str, object] = {"stage": stage, "message": message}
    if current is not None:
        payload["current"] = current
    if total is not None:
        payload["total"] = total
    if run_id:
        payload["runId"] = run_id
    try:
        progress(payload)
    except Exception as exc:
        raise_if_cancelled_callback_exception(exc)
        return


def _graph_quality_gate_failure_message(audit: dict) -> str:
    errors = [str(item) for item in audit.get("quality_errors", []) if str(item)]
    missing_mapped = len(audit.get("missing_mapped_files") or [])
    missing_file_nodes = len(audit.get("missing_file_nodes") or [])
    parts = ["Graph: quality gate failed"]
    if errors:
        parts.append("; ".join(errors))
    details = []
    if missing_mapped:
        details.append(f"missing_mapped_files={missing_mapped}")
    if missing_file_nodes:
        details.append(f"missing_file_nodes={missing_file_nodes}")
    if details:
        parts.append(f"({', '.join(details)})")
    return " ".join(parts)


def _map_graph_tasks_with_progress(
    checkout: Path,
    tasks: list[dict],
    inventory: dict,
    config: object,
    *,
    run: Path,
    progress: ProgressCallback | None,
    progress_label: str = "Graph: mapping shards",
) -> list[dict]:
    if progress is None:
        return map_graph_tasks(checkout, tasks, inventory, config, run=run)
    return map_graph_tasks(
        checkout,
        tasks,
        inventory,
        config,
        run=run,
        progress=_progress_with_run_id(progress, run),
        progress_label=progress_label,
    )


def _run_finders_with_progress(checkout: Path, run: Path, tasks: list[object], config: object, progress: ProgressCallback | None) -> list[dict]:
    if progress is None:
        return run_finders_parallel(checkout, run, tasks, config)
    return run_finders_parallel(checkout, run, tasks, config, progress=_progress_with_run_id(progress, run))


def _run_candidate_verifiers_with_progress(
    candidates: list[dict],
    graph: dict,
    config: object,
    *,
    checkout: Path,
    run: Path,
    progress: ProgressCallback | None,
) -> list[dict]:
    if progress is None:
        return run_candidate_verifiers_parallel(candidates, graph, config, checkout=checkout, run=run)
    return run_candidate_verifiers_parallel(candidates, graph, config, checkout=checkout, run=run, progress=_progress_with_run_id(progress, run))


def _run_repro_workers_with_progress(checkout: Path, run: Path, selected: list[dict], config: object, progress: ProgressCallback | None) -> list[dict]:
    if progress is None:
        return run_repro_workers_parallel(checkout, run, selected, config)
    return run_repro_workers_parallel(checkout, run, selected, config, progress=_progress_with_run_id(progress, run))


def _run_judges_with_progress(
    run: Path,
    selected: list[dict],
    repro_results: list[dict],
    checkout: Path,
    config: object,
    progress: ProgressCallback | None,
) -> list[dict]:
    if progress is None:
        return run_judges_parallel(run, selected, repro_results, checkout, config)
    return run_judges_parallel(run, selected, repro_results, checkout, config, progress=_progress_with_run_id(progress, run))


def _progress_with_run_id(progress: ProgressCallback, run: Path) -> ProgressCallback:
    run_id = run.name

    def emit(value: dict) -> None:
        payload = dict(value) if isinstance(value, dict) else {"message": str(value)}
        payload.setdefault("runId", run_id)
        progress(payload)

    return emit


def build_pipeline_summary(
    *,
    preflight: dict,
    snapshot: object,
    review_units: list[dict],
    unit_coverage: dict,
    snapshot_manifest: dict | None = None,
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
    selected_for_verification: list[dict] | None = None,
    verification_results: list[dict] | None = None,
    graph_audit: dict | None = None,
    agent_graph_audit: dict | None = None,
) -> dict:
    selected_for_verification = selected_for_verification or []
    verification_results = verification_results or []
    graph_audit = graph_audit or {}
    agent_graph_audit = agent_graph_audit or {}
    snapshot_manifest = snapshot_manifest or {}
    finder_blocked = [blocked_summary(item) for item in raw_candidates if item.get("status") == "blocked"]
    repro_blocked = [
        blocked_summary(item)
        for item in repro_results
        if item.get("status") == "blocked" or _nested_status(item, "result") == "blocked"
    ]
    judge_confirmed = [item for item in judge_results if item.get("status") == "confirmed" and item.get("safe_to_show_user") is True]
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
            "snapshotRepo": snapshot_manifest.get("snapshot_repo"),
            "snapshotFiles": snapshot_manifest.get("copied_files_count"),
        },
        "graph": {
            "qualityGate": graph_audit.get("quality_gate"),
            "nodes": graph_audit.get("nodes"),
            "edges": graph_audit.get("edges"),
            "unresolvedRefs": graph_audit.get("unresolved_refs"),
            "reviewFilesMapped": graph_audit.get("review_files_mapped"),
            "reviewFiles": graph_audit.get("review_files"),
            "danglingEdges": graph_audit.get("dangling_edges"),
            "agentAuditStatus": agent_graph_audit.get("status"),
            "agentRepairTasks": len(agent_graph_audit.get("repairs") or []) if isinstance(agent_graph_audit.get("repairs"), list) else 0,
        },
        "reviewUnits": {
            "count": len(review_units),
            "ids": [str(item.get("unit_id") or "") for item in review_units if isinstance(item, dict)],
            "coverage": unit_coverage,
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
            "selectedForVerification": len(selected_for_verification),
            "verificationResults": len(verification_results),
            "verificationReproducible": sum(1 for item in verification_results if item.get("verdict") == "reproducible"),
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


def _graph_repair_tasks(repairs: list[dict], *, start_index: int) -> list[dict]:
    tasks: list[dict] = []
    for repair in repairs:
        if not isinstance(repair, dict) or repair.get("type") != "remap_files":
            continue
        files = [str(path) for path in repair.get("files", []) if str(path)]
        if not files:
            continue
        tasks.append(
            {
                "task_id": f"graph-repair-{start_index + len(tasks):04d}",
                "shard_id": f"repair-{start_index + len(tasks):04d}",
                "mapper_index": 1,
                "files": files,
                "reason": str(repair.get("reason") or "graph audit repair"),
                "double_mapped": False,
            }
        )
    return tasks


def _finder_context_repair_tasks(raw_candidates: list[dict], *, start_index: int) -> list[dict]:
    files: list[str] = []
    for item in raw_candidates:
        result = item.get("result") if isinstance(item, dict) and isinstance(item.get("result"), dict) else {}
        requests = result.get("context_requests") if isinstance(result.get("context_requests"), list) else []
        for request in requests:
            if not isinstance(request, dict):
                continue
            requested = request.get("requested_files") if isinstance(request.get("requested_files"), list) else request.get("files")
            if not isinstance(requested, list):
                continue
            for value in requested:
                rel = safe_relative_path(value)
                if rel and rel not in files:
                    files.append(rel)
    if not files:
        return []
    return [
        {
            "task_id": f"context-repair-{start_index:04d}",
            "shard_id": f"context-repair-{start_index:04d}",
            "mapper_index": 1,
            "files": files[:50],
            "reason": "finder requested bounded graph context repair",
            "double_mapped": False,
        }
    ]


def _finder_context_repair_rerun_plan(raw_candidates: list[dict], old_tasks: list[object], new_tasks: list[object]) -> tuple[list[object], list[dict]]:
    requested_units = _finder_context_request_unit_ids(raw_candidates)
    old_keys = {_finder_task_key(task) for task in old_tasks}
    new_keys = {_finder_task_key(task) for task in new_tasks}
    rerun_tasks = [
        task
        for task in new_tasks
        if _finder_task_unit_id(task) in requested_units or _finder_task_key(task) not in old_keys
    ]
    rerun_keys = {_finder_task_key(task) for task in rerun_tasks}
    kept = [
        item
        for item in raw_candidates
        if _finder_result_key(item) in new_keys and _finder_result_key(item) not in rerun_keys
    ]
    return rerun_tasks, kept


def _finder_context_request_unit_ids(raw_candidates: list[dict]) -> set[str]:
    units: set[str] = set()
    for item in raw_candidates:
        result = item.get("result") if isinstance(item, dict) and isinstance(item.get("result"), dict) else {}
        requests = result.get("context_requests") if isinstance(result.get("context_requests"), list) else []
        if requests:
            unit_id, _focus = _finder_result_key(item)
            if unit_id:
                units.add(unit_id)
    return units


def _finder_result_key(item: object) -> tuple[str, str]:
    source = item if isinstance(item, dict) else {}
    task = source.get("task") if isinstance(source.get("task"), dict) else {}
    result = source.get("result") if isinstance(source.get("result"), dict) else {}
    unit_id = str(task.get("unit_id") or result.get("unit_id") or "")
    focus = str(task.get("focus") or result.get("focus") or "")
    return (unit_id, focus)


def _finder_task_key(task: object) -> tuple[str, str]:
    return (_finder_task_unit_id(task), str(getattr(task, "focus", "") or (task.get("focus") if isinstance(task, dict) else "") or ""))


def _finder_task_unit_id(task: object) -> str:
    return str(getattr(task, "unit_id", "") or (task.get("unit_id") if isinstance(task, dict) else "") or "")


def blocked_summary(item: dict) -> dict:
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    summary = {
        "candidateId": item.get("candidate_id") or result.get("candidate_id") or task.get("unit_id"),
        "focus": task.get("focus"),
        "unitId": task.get("unit_id"),
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
    run_parser.add_argument("--mode", choices=["fast", "standard", "deep"], default="")
    run_parser.add_argument("--scan-mode", choices=["full-cached", "full-strict"], default="")
    args = parser.parse_args(argv)
    checkout = Path(args.checkout)
    if args.command == "init":
        ensure_project_files(checkout)
        print(checkout / ".codereview")
        return 0
    try:
        final = run_review(checkout, mode=args.mode, scan_mode=getattr(args, "scan_mode", ""))
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
