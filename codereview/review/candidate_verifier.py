from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from pathlib import Path

from ..codex_runner import base_env, run_codex_turn
from ..config import ReviewConfig
from ..utils.jsonl import write_json, write_text
from ..utils.paths import ensure_dir, safe_path_component
from ..utils.process import raise_if_cancelled_callback_exception


def run_candidate_verifiers_parallel(
    candidates: list[dict],
    graph: dict,
    config: ReviewConfig,
    checkout: Path | None = None,
    run: Path | None = None,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    limit = min(len(candidates), config.candidates.max_total_for_verification)
    selected = candidates[:limit]
    max_workers = max(1, min(4, getattr(config.finders, "max_workers", 1)))
    results: list[dict | None] = [None] * len(selected)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(verify_candidate, candidate, graph, config, checkout, run): (index, candidate)
            for index, candidate in enumerate(selected)
        }
        completed = 0
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            index, candidate = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = blocked_verification_exception(candidate, graph, exc)
            completed += 1
            _emit_task_progress(
                progress,
                stage="verification",
                message=f"Verification: candidates {completed}/{total}",
                current=completed,
                total=total,
                task_id=candidate.get("issue_id") or candidate.get("candidate_id"),
            )
    return [result for result in results if result is not None]


def blocked_verification_exception(candidate: dict, graph: dict, exc: Exception) -> dict:
    candidate_id = str(candidate.get("issue_id") or candidate.get("candidate_id") or "")
    reason = f"candidate verifier failed before producing a result: {type(exc).__name__}: {exc}"
    return {
        "candidate_id": candidate_id,
        "verdict": "blocked",
        "claim_survived": False,
        "graph_path_valid": False,
        "expected_behavior_supported": False,
        "reproduction": {
            "harness": "",
            "target_test": "",
            "commands": [],
            "expected_signal": "",
            "needs_network": False,
            "estimated_scope": "unknown",
        },
        "rejection_reason": reason,
        "blocked_reason": reason,
        "graph_unresolved_refs": len(graph.get("unresolved_refs", []) or []) if isinstance(graph, dict) else 0,
        "verifier_source": "blocked_exception",
    }


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


def verify_candidate(
    candidate: dict,
    graph: dict,
    config: ReviewConfig,
    checkout: Path | None = None,
    run: Path | None = None,
) -> dict:
    local = local_verify_candidate(candidate, graph, config)
    if checkout is None or run is None:
        return local
    prompt_file = checkout / ".codereview" / "prompts" / "candidate-verifier.md"
    schema = checkout / ".codereview" / "schemas" / "candidate-verification.schema.json"
    if not prompt_file.is_file() or not schema.is_file():
        return local
    candidate_id = str(candidate.get("issue_id") or candidate.get("candidate_id") or "candidate")
    worker = run / "candidate-verifier" / safe_path_component(candidate_id, default="candidate")
    ensure_dir(worker)
    write_json(worker / "candidate.json", candidate)
    prompt = "\n\n".join(
        [
            prompt_file.read_text(encoding="utf-8"),
            "Candidate JSON:",
            json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True),
            "Relevant graph summary JSON:",
            json.dumps(_graph_summary(graph, candidate), ensure_ascii=False, indent=2, sort_keys=True),
            "Local verifier baseline JSON:",
            json.dumps(local, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )
    write_text(worker / "prompt.md", prompt)
    output = worker / "result.json"
    events = worker / "events.jsonl"
    process = run_codex_turn(
        cd=checkout,
        prompt=prompt,
        output_schema=schema,
        output_file=output,
        sandbox="read-only",
        timeout_seconds=config.codex.timeout_seconds,
        config=config.codex,
        env=base_env(checkout, config.codex),
        events_file=events,
    )
    process_payload = {**process.to_dict(), "events_path": str(events)}
    if process.returncode != 0 or not output.is_file():
        return {**local, "process": process_payload, "verifier_source": "local_fallback"}
    try:
        parsed = json.loads(output.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {**local, "process": process_payload, "verifier_source": "local_fallback"}
    if _valid_verification(parsed, candidate_id):
        parsed["process"] = process_payload
        parsed["verifier_source"] = "codex"
        return parsed
    return {**local, "process": process_payload, "verifier_source": "local_fallback"}


def local_verify_candidate(candidate: dict, graph: dict, config: ReviewConfig) -> dict:
    graph_evidence = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    path_summary = graph_evidence.get("path_summary") if isinstance(graph_evidence.get("path_summary"), list) else []
    context_files = graph_evidence.get("context_files") if isinstance(graph_evidence.get("context_files"), list) else []
    expected_source = candidate.get("expected_behavior_source")
    has_expected_source = isinstance(expected_source, list) and any(str(item).strip() for item in expected_source)
    repro = str(candidate.get("minimal_repro_idea") or "").strip()
    network = candidate.get("needs_network") is True
    reasons = []
    if not path_summary:
        reasons.append("missing graph path summary")
    if not context_files:
        reasons.append("missing graph code files")
    if config.candidates.require_expected_behavior_source and not has_expected_source:
        reasons.append("missing expected behavior source")
    if not repro:
        reasons.append("missing local reproduction idea")
    if network:
        reasons.append("reproduction requires network or external service")
    unresolved_count = len(graph.get("unresolved_refs", []) or [])
    verdict = "reproducible" if not reasons else ("unsafe" if network else "reject")
    return {
        "candidate_id": str(candidate.get("issue_id") or candidate.get("candidate_id") or ""),
        "verdict": verdict,
        "claim_survived": verdict == "reproducible",
        "graph_path_valid": bool(path_summary and context_files),
        "expected_behavior_supported": has_expected_source,
        "reproduction": {
            "harness": "existing-test-framework-or-local-script",
            "target_test": "",
            "commands": [],
            "expected_signal": str(candidate.get("actual_behavior_hypothesis") or ""),
            "needs_network": network,
            "estimated_scope": "targeted",
        },
        "rejection_reason": "; ".join(reasons),
        "graph_unresolved_refs": unresolved_count,
        "verifier_source": "local",
    }


def select_reproducible_candidates(candidates: list[dict], verification_results: list[dict], config: ReviewConfig) -> list[dict]:
    by_id = {str(item.get("candidate_id") or ""): item for item in verification_results}
    selected: list[dict] = []
    for candidate in candidates:
        issue_id = str(candidate.get("issue_id") or candidate.get("candidate_id") or "")
        verification = by_id.get(issue_id)
        if not verification or verification.get("verdict") != "reproducible":
            continue
        item = dict(candidate)
        item["candidate_verification"] = verification
        selected.append(item)
        if len(selected) >= config.candidates.max_total_for_reproduction:
            break
    return selected


def _graph_summary(graph: dict, candidate: dict) -> dict:
    graph_evidence = candidate.get("graph_evidence") if isinstance(candidate.get("graph_evidence"), dict) else {}
    files = set(str(path) for path in graph_evidence.get("context_files", []) if str(path))
    nodes = [
        {
            "id": node.get("id"),
            "kind": node.get("kind"),
            "name": node.get("name"),
            "qualified_name": node.get("qualified_name"),
            "file": node.get("file"),
            "span": node.get("span"),
            "attributes": node.get("attributes"),
        }
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and (not files or node.get("file") in files)
    ][:120]
    node_ids = {str(node.get("id") or "") for node in nodes}
    edges = [
        {
            "from": edge.get("from"),
            "to": edge.get("to"),
            "type": edge.get("type"),
            "confidence": edge.get("confidence"),
            "evidence": edge.get("evidence"),
        }
        for edge in graph.get("edges", [])
        if isinstance(edge, dict) and (edge.get("from") in node_ids or edge.get("to") in node_ids)
    ][:160]
    unresolved = [
        ref
        for ref in graph.get("unresolved_refs", [])
        if isinstance(ref, dict) and (not files or ref.get("source_file") in files)
    ][:80]
    return {"nodes": nodes, "edges": edges, "unresolved_refs": unresolved, "path_summary": graph_evidence.get("path_summary") or []}


def _valid_verification(value: object, candidate_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    if str(value.get("candidate_id") or "") != candidate_id:
        return False
    if value.get("verdict") not in {"reject", "reproducible", "blocked", "unsafe", "needs_graph_repair"}:
        return False
    return isinstance(value.get("reproduction"), dict)
