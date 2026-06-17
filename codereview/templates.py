from __future__ import annotations

import json
from pathlib import Path


FINDER_FOCI = [
    "correctness",
    "security_auth_dataflow",
    "api_contract",
    "state_concurrency_resource",
    "test_repro",
]


def ensure_project_files(checkout: Path) -> None:
    root = checkout / ".codereview"
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "codegraph-index").mkdir(parents=True, exist_ok=True)
    config = root / "config.json"
    if not config.is_file():
        config.write_text(
            json.dumps(
                {
                    "mode": "standard",
                    "codegraph": {"command": "codegraph", "optional_sync": True},
                    "codex": {"command": "codex", "reasoning_effort": "high"},
                    "finders": {"enabled": True, "max_workers": 4},
                    "repro": {"enabled": True, "max_workers": 2},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    schemas = {
        "finder_result.schema.json": finder_result_schema(),
        "repro_result.schema.json": repro_result_schema(),
        "judge_result.schema.json": judge_result_schema(),
        "final_report.schema.json": final_report_schema(),
    }
    for name, schema in schemas.items():
        path = root / "schemas" / name
        if not path.is_file():
            path.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
    for focus in FINDER_FOCI:
        path = root / "prompts" / f"finder_{focus}.md"
        if not path.is_file():
            path.write_text(finder_prompt(focus), encoding="utf-8")
    prompts = {
        "repro_worker.md": REPRO_WORKER_PROMPT,
        "judge.md": JUDGE_PROMPT,
        "final_reporter.md": FINAL_REPORTER_PROMPT,
    }
    for name, prompt in prompts.items():
        path = root / "prompts" / name
        if not path.is_file():
            path.write_text(prompt, encoding="utf-8")


def finder_result_schema() -> dict:
    return {
        "type": "object",
        "required": ["candidates"],
        "additionalProperties": False,
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "issue_id",
                        "title",
                        "severity",
                        "category",
                        "graph_evidence",
                        "code_evidence",
                        "trigger_condition",
                        "expected_behavior",
                        "actual_behavior_hypothesis",
                        "minimal_repro_idea",
                        "needs_network",
                    ],
                    "additionalProperties": True,
                    "properties": {
                        "issue_id": {"type": "string"},
                        "title": {"type": "string"},
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                        "category": {"type": "string"},
                        "summary": {"type": "string"},
                        "graph_evidence": {},
                        "code_evidence": {"type": "array"},
                        "trigger_condition": {"type": "string"},
                        "expected_behavior": {"type": "string"},
                        "actual_behavior_hypothesis": {"type": "string"},
                        "minimal_repro_idea": {"type": "string"},
                        "needs_network": {"type": "boolean"},
                    },
                },
            }
        },
    }


def repro_result_schema() -> dict:
    return {
        "type": "object",
        "required": ["candidate_id", "reproduced", "command", "exit_code", "log_path", "observable", "files_written", "limitations"],
        "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string"},
            "reproduced": {"type": "boolean"},
            "command": {"type": "string"},
            "exit_code": {"type": "integer"},
            "log_path": {"type": "string"},
            "observable": {"type": "string"},
            "files_written": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
    }


def judge_result_schema() -> dict:
    return {
        "type": "object",
        "required": ["candidate_id", "status", "level", "safe_to_show_user", "reason", "evidence_summary", "limitations"],
        "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string"},
            "status": {"type": "string", "enum": ["confirmed", "rejected", "blocked"]},
            "level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"]},
            "safe_to_show_user": {"type": "boolean"},
            "reason": {"type": "string"},
            "evidence_summary": {
                "type": "object",
                "required": ["command", "log_path", "observable"],
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "log_path": {"type": "string"},
                    "observable": {"type": "string"},
                },
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
    }


def final_report_schema() -> dict:
    return {"type": "object", "required": ["confirmed"], "properties": {"confirmed": {"type": "array"}}}


def finder_prompt(focus: str) -> str:
    return f"""You are a graph-verified code review finder focused on {focus}.

Hard gates:
- No graph evidence, no candidate.
- Every candidate must be tied to the supplied CodeGraph context pack.
- Every candidate must include file/line evidence, trigger condition, expected behavior, actual behavior hypothesis, and a local minimal repro idea.
- Do not report style concerns or speculative risks.
- Mark needs_network true when reproduction requires a network, credentials, production service, or external database.

Output JSON only matching finder_result.schema.json.
"""


REPRO_WORKER_PROMPT = """You are a reproduction worker.

Hard rules:
- Work on exactly one candidate from ./candidate.json.
- Write only inside the current worker directory.
- Do not use real credentials, production services, external APIs, or destructive operations.
- Generate and run the smallest local command that can prove or disprove the candidate.
- Save command logs under ./logs.
- If no safe local reproduction is possible, return reproduced=false with limitations.

Output JSON only matching repro_result.schema.json.
"""


JUDGE_PROMPT = """You are the judge for a graph-verified code review candidate.

Confirm only when:
- The reproduction command actually ran.
- Logs exist.
- The observed output proves the candidate claim.
- The repro exercises the graph path or affected behavior.
- The failure is not caused by the generated test harness itself.
- The worker obeyed filesystem boundaries.
- The reproduction does not require real credentials, production services, or destructive operations.

Reject static-only, ambiguous, missing-log, unsupported-network, or boundary-violating results.
Output JSON only matching judge_result.schema.json.
"""


FINAL_REPORTER_PROMPT = """Render only confirmed findings. Do not include rejected or blocked candidates in user-facing findings."""
