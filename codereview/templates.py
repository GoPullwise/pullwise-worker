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
                    "scan": {"mode": "full-cached", "include_untracked": True, "fail_on_source_change": True},
                    "scope": {
                        "exclude": [
                            ".git/**",
                            ".codereview/**",
                            "node_modules/**",
                            "vendor/**",
                            "dist/**",
                            "build/**",
                            "coverage/**",
                            "target/**"
                        ],
                        "max_text_file_bytes": 1000000,
                        "inventory_excluded_files": True,
                    },
                    "graph": {
                        "schema_version": "3",
                        "prompt_version": "graph-v3",
                        "full_inventory": True,
                        "incremental": True,
                        "max_shard_files": 25,
                        "max_shard_bytes": 500000,
                        "large_file_bytes": 120000,
                        "double_map_high_risk": True,
                        "max_repair_rounds": 2,
                        "use_sqlite_index": True,
                        "codex_mappers": True,
                    },
                    "agents": {
                        "graph_map_parallel": 6,
                        "graph_link_parallel": 4,
                        "finder_parallel": 5,
                        "verifier_parallel": 4,
                        "repro_parallel": 3,
                        "judge_parallel": 3,
                        "graph_timeout_seconds": 480,
                        "finder_timeout_seconds": 300,
                        "repro_timeout_seconds": 900,
                        "judge_timeout_seconds": 300,
                    },
                    "review": {
                        "require_baseline_for_every_unit": True,
                        "require_boundary_review": True,
                        "require_global_review": True,
                        "max_context_repair_rounds": 1,
                        "max_candidates_per_finder": 3,
                        "default_upstream_depth": 1,
                        "default_downstream_depth": 1,
                        "high_risk_upstream_depth": 2,
                        "high_risk_downstream_depth": 2,
                        "max_unit_nodes": 100,
                        "max_unit_paths": 30,
                        "max_context_chars": 80000,
                    },
                    "candidates": {
                        "max_per_finder_per_unit": 3,
                        "max_total_for_verification": 60,
                        "max_total_for_reproduction": 20,
                        "require_expected_behavior_source": True,
                    },
                    "context": {"enabled": True, "timeout_seconds": 300},
                    "codex": {"command": "codex", "reasoning_effort": "high"},
                    "finders": {"enabled": True, "max_workers": 4},
                    "scoring": {"min_score_for_repro": 8, "always_repro_severities": ["critical", "high"]},
                    "repro": {"enabled": True, "max_workers": 2, "max_repro": 0, "require_red_green": False},
                    "safety": {"confirmed_only": True},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    schemas = {
        "finder_result.schema.json": finder_result_schema(),
        "context_result.schema.json": context_result_schema(),
        "repo-census.schema.json": repo_census_schema(),
        "graph-shard.schema.json": graph_shard_schema(),
        "graph-link.schema.json": graph_link_schema(),
        "graph-audit.schema.json": graph_audit_schema(),
        "candidate-verification.schema.json": candidate_verification_schema(),
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
        "repo-census.md": REPO_CENSUS_PROMPT,
        "graph-mapper.md": GRAPH_MAPPER_PROMPT,
        "graph-linker.md": GRAPH_LINKER_PROMPT,
        "graph-auditor.md": GRAPH_AUDITOR_PROMPT,
        "graph-repair.md": GRAPH_REPAIR_PROMPT,
        "candidate-verifier.md": CANDIDATE_VERIFIER_PROMPT,
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
        "required": ["unit_id", "focus", "candidates"],
        "additionalProperties": False,
        "properties": {
            "unit_id": {"type": "string"},
            "focus": {"type": "string"},
            "context_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "type": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "requested_files": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                },
            },
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
                            "required": ["unit_id", "context_files", "path_summary"],
                            "description": "List the reviewed unit id and repository-relative files from the supplied context pack.",
                            "additionalProperties": False,
                            "properties": {
                                "unit_id": {"type": "string"},
                                "context_files": {"type": "array", "items": {"type": "string"}},
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
                        "expected_behavior_source": {"type": "array", "items": {"type": "string"}},
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


def repo_census_schema() -> dict:
    return {
        "type": "object",
        "required": ["languages", "packages", "entrypoint_candidates", "high_risk_roots", "shards"],
        "additionalProperties": True,
        "properties": {
            "languages": {"type": "array", "items": {"type": "string"}},
            "packages": {"type": "array", "items": {"type": "object"}},
            "entrypoint_candidates": {"type": "array", "items": {"type": "string"}},
            "high_risk_roots": {"type": "array", "items": {"type": "object"}},
            "shards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["shard_id", "files", "reason"],
                    "additionalProperties": True,
                    "properties": {
                        "shard_id": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }


def graph_shard_schema() -> dict:
    evidence = {
        "type": "object",
        "required": ["file", "start_line", "end_line", "evidence_kind"],
        "additionalProperties": True,
        "properties": {
            "file": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "evidence_kind": {"type": "string"},
            "reason": {"type": "string"},
            "content_hash": {"type": "string"},
        },
    }
    node = {
        "type": "object",
        "required": ["id", "kind", "name", "qualified_name", "file", "span", "evidence"],
        "additionalProperties": True,
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "qualified_name": {"type": "string"},
            "language": {"type": "string"},
            "file": {"type": "string"},
            "span": {"type": "object"},
            "signature": {"type": "string"},
            "evidence": {"type": "array", "items": evidence},
        },
    }
    edge = {
        "type": "object",
        "required": ["from", "to", "type", "status", "evidence_kind", "confidence", "evidence"],
        "additionalProperties": True,
        "properties": {
            "id": {"type": "string"},
            "from": {"type": "string"},
            "to": {"type": "string"},
            "type": {"type": "string"},
            "status": {"type": "string"},
            "evidence_kind": {"type": "string"},
            "confidence": {"type": "string"},
            "evidence": {"type": "array", "items": evidence},
        },
    }
    unresolved = {
        "type": "object",
        "required": ["source_node", "reference_kind", "raw_reference", "source_file", "source_line", "reason"],
        "additionalProperties": True,
        "properties": {
            "source_node": {"type": "string"},
            "reference_kind": {"type": "string"},
            "raw_reference": {"type": "string"},
            "source_file": {"type": "string"},
            "source_line": {"type": "integer"},
            "candidate_targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "resolution_hint": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "required": ["nodes", "edges", "unresolved_refs", "coverage", "warnings"],
        "additionalProperties": True,
        "properties": {
            "nodes": {"type": "array", "items": node},
            "edges": {"type": "array", "items": edge},
            "unresolved_refs": {"type": "array", "items": unresolved},
            "coverage": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def graph_link_schema() -> dict:
    return {
        "type": "object",
        "required": ["status"],
        "additionalProperties": True,
        "properties": {
            "status": {"type": "string", "enum": ["resolved", "ambiguous", "still_unresolved", "invalid_reference", "needs_context"]},
            "target": {"type": "string"},
            "requested_files": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    }


def graph_audit_schema() -> dict:
    return {
        "type": "object",
        "required": ["quality_gate", "repairs"],
        "additionalProperties": True,
        "properties": {
            "quality_gate": {"type": "string", "enum": ["passed", "failed"]},
            "repairs": {"type": "array", "items": {"type": "object"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def candidate_verification_schema() -> dict:
    return {
        "type": "object",
        "required": [
            "candidate_id",
            "verdict",
            "claim_survived",
            "graph_path_valid",
            "expected_behavior_supported",
            "reproduction",
            "rejection_reason",
        ],
        "additionalProperties": True,
        "properties": {
            "candidate_id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["reject", "reproducible", "blocked", "unsafe", "needs_graph_repair"]},
            "claim_survived": {"type": "boolean"},
            "graph_path_valid": {"type": "boolean"},
            "expected_behavior_supported": {"type": "boolean"},
            "reproduction": {"type": "object"},
            "rejection_reason": {"type": "string"},
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
- Fill graph_evidence.context_files with repository-relative files from the context pack.
- Every candidate must include file/line evidence, trigger condition, expected behavior, actual behavior hypothesis, and a local minimal repro idea.
- When possible, include expected_behavior_source with tests, contracts, callers, or repository invariants that support the expected behavior.
- Do not report style concerns or speculative risks.
- Mark needs_network true when reproduction requires a network, credentials, production service, or external database.

Output JSON only matching finder_result.schema.json.
Top-level JSON must include unit_id, focus, and candidates.
Each candidate must use candidate_id, dedupe_key, claim, graph_evidence, evidence, trigger_condition,
expected_behavior, actual_behavior_hypothesis, minimal_repro_idea, and repro_likelihood.
"""


REPO_CENSUS_PROMPT = """You are a repository census agent for a graph-verified code review system.

Input will contain a deterministic inventory: paths, sizes, hashes, manifest
files, and generated/excluded policies.

Return JSON only matching repo-census.schema.json.

Tasks:
1. Identify languages, package boundaries, source roots, test roots, manifest files,
   generated roots, high-risk roots, and entrypoint candidates.
2. Plan graph shards so every analyzable source file is assigned exactly once.
3. Keep shards bounded by related package/domain and configured file/byte budgets.

Hard rules:
- Do not modify files.
- Do not invent files that are not in the inventory.
- Do not omit analyzable files from shards.
- Mark uncertain framework entrypoints as candidates, not resolved routes.
"""


GRAPH_MAPPER_PROMPT = """You are a code evidence graph mapper.

You are assigned exactly one repository shard.

Your task:
1. Read every assigned file.
2. Identify meaningful symbols, entrypoints, tests, configuration keys, state stores,
   and external dependencies.
3. Produce graph nodes and graph edges.
4. Include source evidence for every node and edge.
5. Return unresolved references instead of guessing.

Hard rules:
- Do not modify repository files.
- Do not write files directly.
- Do not scan unrelated repository areas.
- Do not invent a symbol or relationship.
- Every resolved edge must cite a source file and line range.
- If a target cannot be uniquely resolved, return unresolved_refs.
- Do not treat naming similarity as proof.
- Use only paths relative to repository root.

Output JSON only, matching graph-shard.schema.json.
"""


GRAPH_LINKER_PROMPT = """You are resolving cross-shard graph references.

Input:
- one unresolved reference
- source evidence
- a bounded candidate target list
- relevant import/export information
- relevant source excerpts

Task:
Determine whether the reference resolves to exactly one target.

Rules:
- Do not select a target only because names match.
- Confirm import path, namespace, receiver type, registry, framework configuration,
  or another concrete mechanism.
- If multiple targets remain possible, return ambiguous.
- If more context is required, return needs_context with exact file paths.
- Never invent a new target.

Output JSON only matching graph-link.schema.json.
"""


GRAPH_AUDITOR_PROMPT = """You are auditing an evidence-backed code graph.

Focus on full-repository coverage, high-risk files, public entrypoints,
authorization paths, state-changing paths, affected tests, cross-boundary edges,
global invariants, and unresolved references used by review units.

Find missing important symbols, missing entrypoint bindings, incorrect graph
edges, missing state sinks, incorrect test mappings, and contradictions between
graph evidence and source.

Do not modify the graph. Return explicit repair tasks only.
Output JSON only matching graph-audit.schema.json.
"""


GRAPH_REPAIR_PROMPT = """You are repairing a bounded gap in an evidence-backed graph.

Read only the requested files and return additional nodes, edges, or unresolved
references with source evidence. Do not rewrite existing graph facts unless the
task explicitly asks you to verify a conflict.

Output JSON only matching graph-shard.schema.json.
"""


CANDIDATE_VERIFIER_PROMPT = """You are an adversarial verifier.

Your job is to try to disprove the candidate before reproduction resources are
spent.

Check:
- whether the graph path is valid
- whether expected behavior is supported
- whether a caller already handles the condition
- whether the candidate duplicates another issue
- whether a safe local reproduction is possible
- whether a safe local reproduction is possible from the snapshot alone

Prefer rejection over speculation.
Return JSON only matching candidate-verification.schema.json.
"""


REPRO_WORKER_PROMPT = """You are a reproduction worker in a graph-verified code review system.

Current directory:
- ./repo is a private copy of the immutable full-repository snapshot.
- ./repro is for extra reproduction scripts.
- ./logs is for command logs.
- ./input_candidate.json contains exactly one candidate.
- ./review-unit.context.md contains the repository context when available.

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
