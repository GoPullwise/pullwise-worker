from __future__ import annotations

import json


def _candidate_id(item: dict) -> str:
    return str(item.get("issue_id") or item.get("candidate_id") or "").strip()


def _index_by_candidate_id(items: list[dict]) -> dict[str, dict]:
    indexed = {}
    for item in items:
        candidate_id = _candidate_id(item)
        if candidate_id:
            indexed[candidate_id] = item
    return indexed


def collect_confirmed(candidates: list[dict], repro_results: list[dict], judge_results: list[dict]) -> list[dict]:
    by_id = _index_by_candidate_id(candidates)
    repro_by_id = _index_by_candidate_id(repro_results)
    confirmed = []
    for judge in judge_results:
        candidate_id = _candidate_id(judge)
        if judge.get("status") == "confirmed" and judge.get("safe_to_show_user") is True:
            confirmed.append({"candidate": by_id.get(candidate_id, {}), "repro": repro_by_id.get(candidate_id, {}), "judge": judge})
    return confirmed


def collect_rejected(candidates: list[dict], repro_results: list[dict], judge_results: list[dict]) -> list[dict]:
    confirmed_ids = {
        _candidate_id(item)
        for item in judge_results
        if item.get("status") == "confirmed" and item.get("safe_to_show_user") is True
    }
    rejected_by_id: dict[str, str] = {}
    for judge in judge_results:
        if judge.get("status") == "confirmed" and judge.get("safe_to_show_user") is True:
            continue
        candidate_id = _candidate_id(judge)
        if candidate_id:
            rejected_by_id[candidate_id] = str(judge.get("reason") or "not confirmed by judge")
    for candidate in candidates:
        issue_id = _candidate_id(candidate)
        if issue_id and issue_id not in confirmed_ids and issue_id not in rejected_by_id:
            rejected_by_id[issue_id] = "not confirmed by judge"
    return [{"candidate_id": candidate_id, "reason": reason} for candidate_id, reason in rejected_by_id.items()]


def render_final_report(
    confirmed: list[dict],
    rejected: list[dict],
    *,
    blocked: int = 0,
    run_id: str,
    mode: str,
    graph_schema: str = "v3",
    coverage: dict | None = None,
    snapshot: dict | None = None,
) -> str:
    coverage = coverage or {}
    snapshot = snapshot or {}
    lines = [
        "# Full-Repository Graph-Verified Code Review",
        "",
        "Scope: full-repository snapshot",
        f"Run ID: {run_id}",
        f"Mode: {mode}",
        f"Graph schema: {graph_schema}",
        f"Snapshot files: {snapshot.get('copied_files_count', 0)}",
        "",
        "## Coverage",
        "",
        f"Inventory files: {coverage.get('inventory_files', 0)}",
        f"Analyzed files: {coverage.get('analyzed_files', 0)}",
        f"Review units: {coverage.get('review_units', 0)}",
        f"Baseline reviewed units: {coverage.get('baseline_reviewed_units', 0)}",
        f"Production symbols covered: {coverage.get('covered_production_symbols', 0)}/{coverage.get('production_symbols', 0)}",
        f"Cross-boundary reviews: {coverage.get('cross_boundary_reviews', 0)}",
        f"Global invariant reviews: {coverage.get('global_invariant_reviews', 0)}",
        f"Critical unresolved graph edges: {coverage.get('critical_unresolved_graph_edges', 0)}",
        "",
        "## Findings",
        "",
        f"Confirmed findings: {len(confirmed)}",
        f"Rejected candidates: {len(rejected)}",
        f"Blocked candidates: {blocked}",
        "",
        "---",
        "",
    ]
    if not confirmed:
        lines.extend(
            [
                "No confirmed findings.",
                _coverage_status_line(coverage),
                "",
            ]
        )
    for index, item in enumerate(confirmed, start=1):
        candidate = item.get("candidate") or {}
        judge = item.get("judge") or {}
        repro = item.get("repro") or {}
        evidence = judge.get("evidence_summary") if isinstance(judge.get("evidence_summary"), dict) else {}
        lines.extend(
            [
                f"## {index}. {candidate.get('title') or candidate.get('claim') or candidate.get('issue_id')}",
                "",
                f"Severity: {candidate.get('severity') or 'medium'}",
                f"Category: {candidate.get('category') or 'Correctness'}",
                f"Verification: {_verification_label(judge.get('level'))} reproduced",
                f"Safe to show user: {str(judge.get('safe_to_show_user')).lower()}",
                "",
                "### Summary",
                "",
                str(candidate.get("summary") or candidate.get("claim") or candidate.get("actual_behavior_hypothesis") or ""),
                "",
                "### Graph Evidence",
                "",
                "```text",
                _text(candidate.get("graph_evidence")),
                "```",
                "",
                "### Code Evidence",
                "",
                _bullet_lines(candidate.get("code_evidence") or candidate.get("evidence")),
                "",
                "### Trigger Condition",
                "",
                str(candidate.get("trigger_condition") or ""),
                "",
                "### Expected Behavior",
                "",
                str(candidate.get("expected_behavior") or ""),
                "",
                "### Observed Behavior",
                "",
                str(evidence.get("observable") or ""),
                "",
                "### Reproduction",
                "",
                "Worker:",
                "",
                "```text",
                str(repro.get("worker") or ""),
                "```",
                "",
                "Command:",
                "",
                "```bash",
                str(evidence.get("command") or ""),
                "```",
                "",
                "Observed output:",
                "",
                "```text",
                str(evidence.get("observable") or "")[:1200],
                "```",
                "",
                "### Why This Matters",
                "",
                str(candidate.get("impact") or candidate.get("why_this_matters") or ""),
                "",
                "### Suggested Fix Direction",
                "",
                str(candidate.get("suggested_fix") or candidate.get("fix_direction") or ""),
                "",
                "### Limitations",
                "",
                _bullet_lines(judge.get("limitations") or []),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_debug_report(
    snapshot: object,
    review_units: list[dict],
    raw_candidates: list[dict],
    selected: list[dict],
    repro_results: list[dict],
    judge_results: list[dict],
    summary: dict | None = None,
) -> str:
    rejected = [item for item in judge_results if item.get("status") == "rejected"]
    blocked = [item for item in judge_results if item.get("status") == "blocked"]
    lines = [
        "# Debug Report",
        "",
        "## Pipeline Stats",
        "",
        f"- Repository files: {len(getattr(snapshot, 'files', []) or [])}",
        f"- Review units: {len(review_units)}",
        f"- Finder raw results: {len(raw_candidates)}",
        f"- Selected for repro: {len(selected)}",
        f"- Repro results: {len(repro_results)}",
        f"- Confirmed: {sum(1 for item in judge_results if item.get('status') == 'confirmed')}",
        f"- Rejected: {len(rejected)}",
        f"- Blocked: {len(blocked)}",
        "",
    ]
    if summary:
        lines.extend(
            [
                "## Pipeline Summary",
                "",
                "```json",
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Finder Results",
            "",
            "```json",
            json.dumps(raw_candidates, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
            "```",
            "",
            "## Repro Results",
            "",
            "```json",
            json.dumps(repro_results, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
            "```",
            "",
            "## Judge Results",
            "",
            "```json",
            json.dumps(judge_results, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
            "```",
        ]
    )
    return "\n".join(lines)


def _text(value: object) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value or "")


def _bullet_lines(value: object) -> str:
    if not isinstance(value, list):
        value = [value] if value else []
    lines = [f"* `{item}`" if isinstance(item, str) else f"* `{json.dumps(item, ensure_ascii=False)}`" for item in value if item]
    return "\n".join(lines) if lines else "* None"


def _verification_label(value: object) -> str:
    text = str(value or "L2")
    return {"L2": "V2", "L3": "V3", "L1": "V1", "L0": "V0"}.get(text, text)


def _coverage_status_line(coverage: dict) -> str:
    review_units = int(coverage.get("review_units") or 0)
    baseline = int(coverage.get("baseline_reviewed_units") or 0)
    production = int(coverage.get("production_symbols") or 0)
    covered = int(coverage.get("covered_production_symbols") or 0)
    critical_unresolved = int(coverage.get("critical_unresolved_graph_edges") or 0)
    if review_units == baseline and production == covered and critical_unresolved == 0:
        return "Full-repository coverage: passed."
    return "Full-repository coverage: incomplete."
