from __future__ import annotations

import json
from pathlib import Path

from .utils.jsonl import write_text
from .utils.paths import ensure_dir


FINDER_FOCI = [
    "correctness",
    "security_auth_dataflow",
    "api_contract",
    "state_concurrency_resource",
    "test_repro",
]


def ensure_project_files(checkout: Path) -> None:
    root = checkout / ".codereview"
    ensure_dir(root / "schemas")
    ensure_dir(root / "prompts")
    ensure_dir(root / "runs")
    config = root / "config.json"
    if config.is_symlink() or not config.is_file():
        write_text(
            config,
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
                        "target_shards": 12,
                        "max_shard_files": 25,
                        "max_shard_bytes": 500000,
                        "large_file_bytes": 120000,
                        "double_map_high_risk": True,
                        "max_repair_rounds": 2,
                        "use_sqlite_index": True,
                        "codex_tool_extractor": True,
                        "tool_extractor_max_rounds": 3,
                        "tool_extractor_timeout_seconds": 180,
                        "codex_census": False,
                        "codex_mappers": False,
                        "codex_linker": False,
                        "codex_graph_audit": False,
                        "mapper_subagent_limit": 6,
                        "map_parallel": 2,
                        "graph_timeout_seconds": 960,
                    },
                    "agents": {
                        "graph_map_parallel": 2,
                        "graph_link_parallel": 4,
                        "finder_parallel": 6,
                        "finder_turn_parallel": 1,
                        "finder_max_turns_per_scan": 3,
                        "finder_max_jobs_per_subagent": 18,
                        "verifier_parallel": 4,
                        "repro_parallel": 3,
                        "judge_parallel": 3,
                        "graph_timeout_seconds": 960,
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
                        "max_unit_nodes": 500,
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
                    "codex": {"command": "codex", "reasoning_effort": "high", "max_input_chars": 0},
                    "finders": {"enabled": True, "max_workers": 6, "turn_parallel": 1, "max_turns_per_scan": 3, "max_jobs_per_subagent": 18},
                    "scoring": {"min_score_for_repro": 8, "always_repro_severities": ["critical", "high"]},
                    "repro": {"enabled": True, "max_workers": 2, "max_repro": 0, "require_red_green": False},
                    "safety": {"confirmed_only": True},
                },
                indent=2,
            ),
        )
    schemas = {
        "finder_result.schema.json": codex_output_schema(finder_result_schema()),
        "finder-batch.schema.json": codex_output_schema(finder_batch_schema()),
        "context_result.schema.json": codex_output_schema(context_result_schema()),
        "repo-census.schema.json": codex_output_schema(repo_census_schema()),
        "graph-extractor-tool.schema.json": codex_output_schema(graph_extractor_tool_schema()),
        "graph-shard.schema.json": codex_output_schema(graph_shard_schema()),
        "graph-shard-batch.schema.json": codex_output_schema(graph_shard_batch_schema()),
        "graph-link.schema.json": codex_output_schema(graph_link_schema()),
        "graph-audit.schema.json": codex_output_schema(graph_audit_schema()),
        "candidate-verification.schema.json": codex_output_schema(candidate_verification_schema()),
        "repro_result.schema.json": codex_output_schema(repro_result_schema()),
        "judge_result.schema.json": codex_output_schema(judge_result_schema()),
        "final_report.schema.json": codex_output_schema(final_report_schema()),
    }
    for name, schema in schemas.items():
        path = root / "schemas" / name
        write_text(path, json.dumps(schema, indent=2, sort_keys=True))
    for focus in FINDER_FOCI:
        path = root / "prompts" / f"finder_{focus}.md"
        if path.is_symlink() or not path.is_file():
            write_text(path, finder_prompt(focus))
    prompts = {
        "finder-batch-coordinator.md": FINDER_BATCH_COORDINATOR_PROMPT,
        "repo-census.md": REPO_CENSUS_PROMPT,
        "graph-tool-extractor.md": GRAPH_TOOL_EXTRACTOR_PROMPT,
        "graph-mapper.md": GRAPH_MAPPER_PROMPT,
        "graph-mapper-coordinator.md": GRAPH_MAPPER_COORDINATOR_PROMPT,
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
        if path.is_symlink() or not path.is_file():
            write_text(path, prompt)


def codex_output_schema(schema: dict) -> dict:
    return _codex_output_schema_value(schema)


def _codex_output_schema_value(value: object) -> object:
    if isinstance(value, list):
        return [_codex_output_schema_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {str(key): _codex_output_schema_value(item) for key, item in value.items()}
    if "object" in _schema_type_names(normalized.get("type")):
        normalized["additionalProperties"] = False
        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["required"] = list(properties.keys())
    return normalized


def _schema_type_names(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


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
                        "expected_behavior_source",
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
                        "expected_behavior_source": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                        "repro_likelihood": {"type": "string", "enum": ["high", "medium", "low"]},
                        "needs_network": {"type": "boolean"},
                        "notes": {"type": "string"},
                    },
                },
            }
        },
    }


def finder_batch_schema() -> dict:
    return {
        "type": "object",
        "required": ["results", "warnings"],
        "additionalProperties": False,
        "properties": {
            "results": {"type": "array", "items": finder_result_schema()},
            "warnings": {"type": "array", "items": {"type": "string"}},
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
    package = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "root": {"type": "string"},
            "source_roots": {"type": "array", "items": {"type": "string"}},
            "test_roots": {"type": "array", "items": {"type": "string"}},
            "manifest_files": {"type": "array", "items": {"type": "string"}},
        },
    }
    high_risk_root = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "required": ["languages", "packages", "entrypoint_candidates", "high_risk_roots", "shards"],
        "additionalProperties": True,
        "properties": {
            "languages": {"type": "array", "items": {"type": "string"}},
            "packages": {"type": "array", "items": package},
            "entrypoint_candidates": {"type": "array", "items": {"type": "string"}},
            "high_risk_roots": {"type": "array", "items": high_risk_root},
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
    return graph_shard_result_schema(include_identity=False)


def graph_extractor_tool_schema() -> dict:
    return {
        "type": "object",
        "required": ["script", "summary", "assumptions"],
        "additionalProperties": True,
        "properties": {
            "script": {
                "type": "string",
                "description": "A complete Python 3.10+ extractor script that accepts --repo, --inventory, and --output.",
            },
            "summary": {"type": "string"},
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
    }


def graph_shard_batch_schema() -> dict:
    return {
        "type": "object",
        "required": ["results", "warnings"],
        "additionalProperties": True,
        "properties": {
            "results": {"type": "array", "items": graph_shard_result_schema(include_identity=True)},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def graph_shard_result_schema(*, include_identity: bool) -> dict:
    span = {
        "type": "object",
        "properties": {
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
    }
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
            "span": span,
            "signature": {"type": "string"},
            "attributes": {"type": "array", "items": {"type": "string"}},
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
    properties = {
        "nodes": {"type": "array", "items": node},
        "edges": {"type": "array", "items": edge},
        "unresolved_refs": {"type": "array", "items": unresolved},
        "coverage": {
            "type": "object",
            "properties": {
                "assigned_files": {"type": "array", "items": {"type": "string"}},
                "mapped_files": {"type": "array", "items": {"type": "string"}},
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    }
    required = ["nodes", "edges", "unresolved_refs", "coverage", "warnings"]
    if include_identity:
        identity = {
            "task_id": {"type": "string"},
            "shard_id": {"type": "string"},
            "mapper_index": {"type": "integer"},
            "files": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string"},
        }
        properties = {**identity, **properties}
        required = [*identity, *required]
    return {
        "type": "object",
        "required": required,
        "additionalProperties": True,
        "properties": properties,
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
    repair = {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "required": ["quality_gate", "repairs"],
        "additionalProperties": True,
        "properties": {
            "quality_gate": {"type": "string", "enum": ["passed", "failed"]},
            "repairs": {"type": "array", "items": repair},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def candidate_verification_schema() -> dict:
    reproduction = {
        "type": "object",
        "properties": {
            "harness": {"type": "string"},
            "target_test": {"type": "string"},
            "commands": {"type": "array", "items": {"type": "string"}},
            "expected_signal": {"type": "string"},
            "needs_network": {"type": "boolean"},
            "estimated_scope": {"type": "string"},
        },
    }
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
            "reproduction": reproduction,
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
- Include expected_behavior_source with tests, contracts, callers, or repository invariants that support the expected behavior.
- Do not report style concerns or speculative risks.
- Mark needs_network true when reproduction requires a network, credentials, production service, or external database.

Output JSON only matching finder_result.schema.json.
Top-level JSON must include unit_id, focus, and candidates.
Each candidate must use candidate_id, dedupe_key, claim, graph_evidence, evidence, trigger_condition,
expected_behavior, expected_behavior_source, actual_behavior_hypothesis, minimal_repro_idea, and repro_likelihood.
"""


FINDER_BATCH_COORDINATOR_PROMPT = """You are a finder coordinator for a full-repository GraphVerified review.

You are running inside exactly one Codex app-server turn. Do not ask the caller
to start another Codex process, and do not run nested codex commands.

You will receive finder jobs and deterministic job_groups built by the worker.
Each job has unit_id, focus, group_id, module_key, unit_type, review_pass,
risk_tags, and context_pack_id. Each job_group is the unit of subagent
assignment and contains related jobs grouped by business module, files, and
graph connectivity. The payload also includes context_packs keyed by unit_id
and the focus-specific finder prompt text for each focus.

Your task:
1. Spawn subagents inside this Codex session to review job_groups concurrently.
2. Use at most finder_subagent_limit subagents at one time.
3. Assign one job_group to each subagent. A subagent must process every job
   listed in that group.
4. Do not create extra waves for individual jobs. The worker has already shaped
   this turn to the available subagent budget.
5. Wait for all subagents to finish.
6. Return one finder result per input job, preserving unit_id and focus.

Each subagent must follow the focus-specific finder prompt for its job and use
only context_packs[job.context_pack_id] plus repository files explicitly
referenced by that context. Reuse module-level code reading across jobs with the
same module_key. If required context is missing, return context_requests with
exact repository-relative files instead of inventing evidence.

If one job_group is broad, first build a concise module map from the supplied
context packs, then review the highest-risk paths inside that group. If a single
job is blocked, return an empty candidates list plus context_requests or a
warning for that job. Do not let one long-tail job block the whole batch.

Hard rules:
- Do not modify repository files.
- Do not write files directly.
- Do not scan unrelated repository areas.
- No repository context evidence, no candidate.
- Do not report style concerns or speculative risks.
- Each result must include unit_id, focus, context_requests, and candidates.

Output JSON only, matching finder-batch.schema.json.
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
4. Use shard_policy.target_shards as the soft target. Keep primary shard count at
   or below shard_policy.mapper_subagent_limit when the file/byte budgets and
   large-file isolation allow it.

Hard rules:
- Do not modify files.
- Do not invent files that are not in the inventory.
- Do not omit analyzable files from shards.
- Mark uncertain framework entrypoints as candidates, not resolved routes.
"""


GRAPH_TOOL_EXTRACTOR_PROMPT = """You are writing a repository-specific graph extractor tool.

Codex is not the graph generator. Your job is to write a deterministic Python
3.10+ script that the worker will execute against an immutable repository
snapshot. The script must inspect source files and emit graph-shard JSON.

The generated script must:
- Use only the Python standard library.
- Accept --repo, --inventory, and --output arguments.
- Treat --repo as read-only and never modify repository files.
- Never import or execute the target project's modules.
- Read files as text, parse with static tools such as ast/json/tomllib/regex,
  and use repository manifests/configuration to infer framework structure.
- Produce graph-shard JSON with nodes, edges, unresolved_refs, coverage, and
  warnings.
- Include every analyzable inventory file in coverage.assigned_files and
  coverage.mapped_files when it was inspected.
- Add source evidence for every node and edge using repository-relative paths
  and real line ranges.
- Return unresolved_refs instead of guessing relationships that the script
  cannot prove.

If prior execution feedback is provided, repair the script based on that
feedback. Do not ask the caller to run commands; the worker will run the script
and send the result back if it fails.

Return JSON only matching graph-extractor-tool.schema.json.
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


GRAPH_MAPPER_COORDINATOR_PROMPT = """You are a graph mapper coordinator for a full-repository GraphVerified review.

You are running inside exactly one Codex app-server turn. Do not ask the caller
to start another Codex process.

You will receive several independent mapper jobs. Each job has task_id, shard_id,
mapper_index, files, reason, double_mapped, and files_metadata.

Your task:
1. Spawn subagents inside this Codex session to map the jobs concurrently.
2. Use at most mapper_subagent_limit subagents at one time.
3. Assign one mapper job to each subagent.
4. If there are more jobs than mapper_subagent_limit, run additional waves inside
   this same Codex session.
5. Wait for all subagents to finish.
6. Return one graph shard result per input job, preserving task_id identity.

Each subagent must:
- Read every file assigned to its job.
- Stay within its assigned files except for direct import/export evidence needed
  to avoid inventing an edge.
- Identify meaningful symbols, entrypoints, tests, configuration keys, state
  stores, and external dependencies.
- Produce graph nodes and graph edges with source evidence for every node and edge.
- Return unresolved_refs instead of guessing.

Hard rules:
- Do not modify repository files.
- Do not write files directly.
- Do not scan unrelated repository areas.
- Do not invent a symbol or relationship.
- Every resolved edge must cite a source file and line range.
- If a target cannot be uniquely resolved, return unresolved_refs.
- Do not treat naming similarity as proof.
- Use only paths relative to repository root.
- Every result must echo task_id, shard_id, mapper_index, files, and status.
- Every result coverage.assigned_files must match the job files.
- Every mapped file must appear in coverage.mapped_files.

Output JSON only, matching graph-shard-batch.schema.json.
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
