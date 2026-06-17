from __future__ import annotations

import json


def collect_confirmed(candidates: list[dict], repro_results: list[dict], judge_results: list[dict]) -> list[dict]:
    by_id = {str(item.get("issue_id") or ""): item for item in candidates}
    repro_by_id = {str(item.get("candidate_id") or ""): item for item in repro_results}
    confirmed = []
    for judge in judge_results:
        candidate_id = str(judge.get("candidate_id") or "")
        if judge.get("status") == "confirmed" and judge.get("safe_to_show_user") is True:
            confirmed.append({"candidate": by_id.get(candidate_id, {}), "repro": repro_by_id.get(candidate_id, {}), "judge": judge})
    return confirmed


def collect_rejected(candidates: list[dict], repro_results: list[dict], judge_results: list[dict]) -> list[dict]:
    confirmed_ids = {str(item.get("candidate_id") or "") for item in judge_results if item.get("status") == "confirmed"}
    rejected = []
    for candidate in candidates:
        issue_id = str(candidate.get("issue_id") or "")
        if issue_id not in confirmed_ids:
            rejected.append({"candidate_id": issue_id, "reason": "not confirmed by judge"})
    for judge in judge_results:
        if judge.get("status") != "confirmed":
            rejected.append({"candidate_id": judge.get("candidate_id"), "reason": judge.get("reason")})
    return rejected


def render_final_report(confirmed: list[dict], rejected: list[dict], *, base_ref: str, head_ref: str, run_id: str, mode: str) -> str:
    lines = [
        "# Graph-Verified Code Review Report",
        "",
        f"Base: {base_ref}",
        f"Head: {head_ref}",
        f"Run ID: {run_id}",
        f"Mode: {mode}",
        "",
        f"Confirmed findings: {len(confirmed)}",
        f"Rejected candidates: {len(rejected)}",
        "Blocked candidates: 0",
        "",
        "---",
        "",
    ]
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
                f"Verification: {judge.get('level') or 'L2'} reproduced",
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


def render_debug_report(diff: object, slices: list[dict], raw_candidates: list[dict], selected: list[dict], repro_results: list[dict], judge_results: list[dict]) -> str:
    rejected = [item for item in judge_results if item.get("status") == "rejected"]
    blocked = [item for item in judge_results if item.get("status") == "blocked"]
    return "\n".join(
        [
            "# Debug Report",
            "",
            "## Pipeline Stats",
            "",
            f"- Changed files: {len(getattr(diff, 'changed_files', []) or [])}",
            f"- Slices: {len(slices)}",
            f"- Finder raw results: {len(raw_candidates)}",
            f"- Selected for repro: {len(selected)}",
            f"- Repro results: {len(repro_results)}",
            f"- Confirmed: {sum(1 for item in judge_results if item.get('status') == 'confirmed')}",
            f"- Rejected: {len(rejected)}",
            f"- Blocked: {len(blocked)}",
            "",
            "## Judge Results",
            "",
            "```json",
            json.dumps(judge_results, ensure_ascii=False, indent=2, sort_keys=True)[:20000],
            "```",
        ]
    )


def _text(value: object) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value or "")


def _bullet_lines(value: object) -> str:
    if not isinstance(value, list):
        value = [value] if value else []
    lines = [f"* `{item}`" if isinstance(item, str) else f"* `{json.dumps(item, ensure_ascii=False)}`" for item in value if item]
    return "\n".join(lines) if lines else "* None"
