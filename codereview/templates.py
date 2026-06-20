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
    config = root / "config.json"
    if not config.is_file():
        config.write_text(
            json.dumps(
                {
                    "mode": "standard",
                    "context": {"enabled": True, "timeout_seconds": 300},
                    "codex": {"command": "codex", "reasoning_effort": "high"},
                    "finders": {"enabled": True, "max_workers": 4},
                    "scoring": {"min_score_for_repro": 8, "always_repro_severities": ["critical", "high"]},
                    "repro": {"enabled": True, "max_workers": 2, "max_repro": 0, "require_red_green": False},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    schemas = {
        "finder_result.schema.json": finder_result_schema(),
        "context_result.schema.json": context_result_schema(),
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
        "required": ["slice_id", "focus", "candidates"],
        "additionalProperties": False,
        "properties": {
            "slice_id": {"type": "string"},
            "focus": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "candidate_id",
                        "dedupe_key",
                        "severity",
                        "category",
                        "confidence",
                        "claim",
                        "graph_evidence",
                        "evidence",
                        "trigger_condition",
                        "expected_behavior",
                        "actual_behavior_hypothesis",
                        "minimal_repro_idea",
                        "repro_likelihood",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "dedupe_key": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "correctness",
                                "security_auth_dataflow",
                                "api_contract",
                                "state_concurrency_resource",
                                "test_repro",
                            ],
                        },
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "claim": {"type": "string"},
                        "graph_evidence": {
                            "type": "object",
                            "required": ["slice_id", "codegraph_files", "path_summary"],
                            "description": "codegraph_files is a legacy field name; list repository-relative files from the supplied context pack.",
                            "additionalProperties": False,
                            "properties": {
                                "slice_id": {"type": "string"},
                                "codegraph_files": {"type": "array", "items": {"type": "string"}},
                                "path_summary": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["file", "lines", "why_it_matters"],
                                "additionalProperties": False,
                                "properties": {
                                    "file": {"type": "string"},
                                    "lines": {"type": "string"},
                                    "why_it_matters": {"type": "string"},
                                },
                            },
                        },
                        "trigger_condition": {"type": "string"},
                        "expected_behavior": {"type": "string"},
                        "actual_behavior_hypothesis": {"type": "string"},
                        "minimal_repro_idea": {"type": "string"},
                        "repository_tests": {"type": "array", "items": {"type": "string"}},
                        "repro_likelihood": {"type": "string", "enum": ["high", "medium", "low"]},
                        "needs_network": {"type": "boolean"},
                        "notes": {"type": "string"},
                    },
                },
            }
        },
    }


def context_result_schema() -> dict:
    path_location = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "symbol": {"type": "string"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "required": ["summary", "files", "path_summary", "nodes", "edges", "callers", "callees", "impact"],
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "array", "items": {"type": "string"}},
            "files": {"type": "array", "items": {"type": "string"}},
            "path_summary": {"type": "array", "items": {"type": "string"}},
            "nodes": {"type": "array", "items": path_location},
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "kind": {"type": "string"},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                    },
                },
            },
            "callers": {"type": "array", "items": path_location},
            "callees": {"type": "array", "items": path_location},
            "impact": {"type": "array", "items": path_location},
        },
    }


def repro_result_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "candidate_id",
            "status",
            "level",
            "summary",
            "commands_run",
            "files_written",
            "proof",
            "graph_path_exercised",
            "why_valid",
            "why_not_reproduced",
            "safety_notes",
        ],
        "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["reproduced", "not_reproduced", "blocked", "unsafe", "ambiguous", "harness_error", "timeout"],
            },
            "level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"]},
            "summary": {"type": "string"},
            "commands_run": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["cmd", "cwd", "exit_code", "log_path"],
                    "additionalProperties": False,
                    "properties": {
                        "cmd": {"type": "string"},
                        "cwd": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "log_path": {"type": "string"},
                        "duration_ms": {"type": "integer"},
                    },
                },
            },
            "files_written": {"type": "array", "items": {"type": "string"}},
            "proof": {
                "type": "object",
                "required": ["type", "expected", "actual", "log_excerpt"],
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["failing_test", "runtime_output", "assertion", "red_green", "static_check", "other", "none"],
                    },
                    "expected": {"type": "string"},
                    "actual": {"type": "string"},
                    "log_excerpt": {"type": "string"},
                },
            },
            "graph_path_exercised": {"type": "boolean"},
            "touched_symbols": {"type": "array", "items": {"type": "string"}},
            "environment": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "network_used": {"type": "boolean"},
                    "dependencies_installed": {"type": "boolean"},
                },
            },
            "why_valid": {"type": "string"},
            "why_not_reproduced": {"type": "string"},
            "safety_notes": {"type": "string"},
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
- No repository context evidence, no candidate.
- Use the supplied repository context pack as review input and audit evidence.
- Every candidate must be tied to concrete repository context from the supplied context pack or files it references.
- Fill graph_evidence.codegraph_files with repository-relative files from the context pack; this is a legacy field name.
- Every candidate must include file/line evidence, trigger condition, expected behavior, actual behavior hypothesis, and a local minimal repro idea.
- Do not report style concerns or speculative risks.
- Mark needs_network true when reproduction requires a network, credentials, production service, or external database.

Output JSON only matching finder_result.schema.json.
Top-level JSON must include slice_id, focus, and candidates.
Each candidate must use candidate_id, dedupe_key, claim, graph_evidence, evidence, trigger_condition,
expected_behavior, actual_behavior_hypothesis, minimal_repro_idea, and repro_likelihood.
"""


REPRO_WORKER_PROMPT = """You are a reproduction worker in a graph-verified code review system.

Current directory:
- ./repo is a private copy of the repository.
- ./repro is for extra reproduction scripts.
- ./logs is for command logs.
- ./input_candidate.json contains exactly one candidate.
- ./slice.context.md contains the repository context when available.

Hard rules:
- Work on exactly one candidate.
- Write only inside ./repo, ./repro, ./logs, or ./result.json.
- Do not modify the original checkout.
- Do not use real credentials, production services, external APIs, or destructive operations.
- Prefer existing tests, repository tests, local mocks, fixtures, and offline scripts.
- Save full command output under ./logs.
- Do not claim reproduced unless command output proves the candidate claim and exercises the graph path.
- If no safe local reproduction is possible, return blocked, unsafe, ambiguous, harness_error, or not_reproduced.

Output JSON only matching repro_result.schema.json.
"""


JUDGE_PROMPT = """You are the judge for a graph-verified code review candidate.

Confirm only when:
- The reproduction command actually ran.
- Logs exist.
- The observed output proves the candidate claim.
- The repro exercises the graph path or repository behavior.
- The failure is not caused by the generated test harness itself.
- The worker obeyed filesystem boundaries.
- The reproduction does not require real credentials, production services, or destructive operations.

Reject static-only, ambiguous, missing-log, unsupported-network, or boundary-violating results.
Output JSON only matching judge_result.schema.json.
"""


FINAL_REPORTER_PROMPT = """Render only confirmed findings. Do not include rejected or blocked candidates in user-facing findings."""
